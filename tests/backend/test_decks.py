from __future__ import annotations

import json

import pytest_asyncio
from sqlalchemy import event

from mpc_forge.clients.import_sites import list_supported_sites, resolve_site
from mpc_forge.clients.import_sites.moxfield import _payload_to_text
from mpc_forge.db import engine, session_scope
from mpc_forge.models import CustomArt, Deck, DeckCard, DFCPair, PrintingCache
from mpc_forge.services.art import custom_art
from mpc_forge.services.cards import printings
from mpc_forge.services.decks import deck_covers, deck_validation
from mpc_forge.services.decks.deck_covers import pick_cover_card
from mpc_forge.services.decks.decklist_parser import parse_plain_decklist
from mpc_forge.services.decks.importer import _revert_dfc_backs_to_fronts
from mpc_forge.services.printing import build_progress
from mpc_forge.utils.iterables import chunked

HTML = {"accept": "text/html"}


def _card(deck: dict, name: str) -> dict:
    return next(c for c in deck["cards"] if c["name"] == name)


async def _events(client, deck_id: int) -> list[dict]:
    return (await client.get(f"/api/decks/{deck_id}/activity")).json()


class TestImportSources:
    def test_sites_are_resolved_by_hostname(self):
        cases = {
            "https://www.moxfield.com/decks/abc": "MoxfieldSite",
            "https://moxfield.com/decks/abc": "MoxfieldSite",
            "https://archidekt.com/decks/12345": "ArchidektSite",
            "https://cubecobra.com/cube/list/xyz": "CubeCobraSite",
            "https://deckstats.net/decks/1234/5678-my-deck": "DeckstatsSite",
            "https://mtggoldfish.com/deck/123": "MTGGoldfishSite",
            "https://scryfall.com/@user/decks/abc": "ScryfallSite",
            "https://tappedout.net/mtg-decks/x/": "TappedOutSite",
        }
        for url, expected in cases.items():
            site = resolve_site(url)
            assert site is not None and site.__name__ == expected, url
        assert all(resolve_site(u) is None for u in ("https://random.example/x", "not-a-url", ""))
        assert {s["key"] for s in list_supported_sites()} == {
            "moxfield",
            "archidekt",
            "cubecobra",
            "deckstats",
            "mtggoldfish",
            "scryfall",
            "tappedout",
        }

    async def test_supported_sites_endpoint(self, client):
        keys = {s["key"] for s in (await client.get("/api/decks/import/supported-sites")).json()}
        assert {"moxfield", "archidekt"} <= keys

    def test_moxfield_payload_becomes_a_decklist(self):
        text = _payload_to_text(
            {
                "boards": {
                    "commanders": {
                        "cards": {
                            "c1": {
                                "quantity": 1,
                                "card": {"name": "Atraxa", "set": "c21", "cn": "2"},
                            }
                        }
                    },
                    "mainboard": {
                        "cards": {
                            "m1": {
                                "quantity": 1,
                                "card": {"name": "Sol Ring", "set": "cmm", "cn": "451"},
                            },
                            "m2": {"quantity": 4, "card": {"name": "Forest"}},
                            "m3": {
                                "quantity": 1,
                                "card": {
                                    "name": "Zanarkand, Ancient Metropolis // Lasting Fayth",
                                    "set": "ffa",
                                    "cn": "193",
                                },
                            },
                            "m4": {
                                "quantity": 1,
                                "card": {"name": "Delver of Secrets // Insectile Aberration"},
                            },
                        }
                    },
                    "sideboard": {"cards": {}},
                }
            }
        )
        for line in (
            "//Commanders",
            "1 Atraxa (c21) 2",
            "1 Sol Ring (cmm) 451",
            "4 Forest",
            "1 Zanarkand, Ancient Metropolis // Lasting Fayth (ffa) 193",
            "1 Delver of Secrets // Insectile Aberration",
        ):
            assert line in text, line
        assert "//Sideboard" not in text and "Insectile Aberration (" not in text

    def test_plain_text_parser_tracks_sections(self):
        by_name = {
            e["name"]: e
            for e in parse_plain_decklist(
                "//Commanders\n1 Atraxa\n//Mainboard\n1 Sol Ring\n//Sideboard\n2 Blood Moon"
            )
        }
        assert [by_name[n]["role"] for n in ("Atraxa", "Sol Ring", "Blood Moon")] == [
            "commander",
            "mainboard",
            "sideboard",
        ]
        by_name = {
            e["name"]: e
            for e in parse_plain_decklist("1 Sol Ring\nSB: 2 Blood Moon\n1 Lightning Bolt")
        }
        assert [by_name[n]["role"] for n in ("Sol Ring", "Blood Moon", "Lightning Bolt")] == [
            "mainboard",
            "sideboard",
            "mainboard",
        ]
        [entry] = parse_plain_decklist("4 Sideboard")
        assert (entry["name"], entry["quantity"]) == ("Sideboard", 4)

    async def test_import_reports_unresolved_lines(self, client):
        r = await client.post(
            "/api/decks/import/text",
            json={"name": "Bad", "text": "1 Sol Ring\n1 Command Tower\n1 Not A Real Card"},
        )
        result = r.json()
        assert result["resolved_count"] == 2 and result["total_entries"] == 3
        [missing] = result["unresolved"]
        assert missing["name"] == "Not A Real Card" and missing["raw_line"] == "1 Not A Real Card"

        r = await client.post("/api/decks/import/text", json={"name": "Ok", "text": "1 Sol Ring"})
        assert r.json()["unresolved"] == []

    async def test_dfc_back_faces_are_imported_as_their_front(self, client):
        async with session_scope() as s:
            s.add(
                DFCPair(
                    front_name="Delver of Secrets",
                    back_name="Insectile Aberration",
                    kind="transform",
                )
            )
            s.add(
                DFCPair(
                    front_name="Zanarkand, Ancient Metropolis",
                    back_name="Lasting Fayth",
                    kind="transform",
                )
            )
        r = await client.post(
            "/api/decks/import/text",
            json={"name": "DFC", "text": "1 Insectile Aberration\n1 Sol Ring"},
        )
        assert r.status_code in (200, 400)

        entries = [
            {"name": "Zanarkand, Ancient Metropolis // Lasting Fayth", "quantity": 1},
            {"name": "Lasting Fayth", "quantity": 1},
        ]
        async with session_scope() as s:
            await _revert_dfc_backs_to_fronts(s, entries)
        assert entries[0]["name"] == "Zanarkand, Ancient Metropolis // Lasting Fayth"
        assert "dfc_reverted_from" not in entries[0]
        assert entries[1]["name"] == "Zanarkand, Ancient Metropolis"
        assert entries[1]["dfc_reverted_from"] == "Lasting Fayth"


class TestExportAndLocalization:
    async def test_decklist_formats(self, client, deck):
        url = f"/api/decks/{deck['id']}/decklist"
        simple = (await client.get(f"{url}?format=simple")).json()["text"]
        assert (
            "1 Sol Ring" in simple and "1 Command Tower" in simple and "(c21)" not in simple.lower()
        )
        assert "1 Sol Ring (c21) 263" in (await client.get(f"{url}?format=with_set")).json()["text"]
        assert "1 Sol Ring (C21) 263" in (await client.get(f"{url}?format=arena")).json()["text"]
        assert (await client.get(f"{url}?format=nonsense")).status_code == 400
        txt = await client.get(f"{url}.txt?format=with_set")
        assert txt.headers["content-type"].startswith("text/plain") and "Sol Ring" in txt.text

    async def test_localization(self, client, deck):
        langs = (await client.get("/api/decks/_/supported-langs")).json()
        assert (langs["es"], langs["en"], langs["ja"]) == ("Español", "English", "日本語")

        url = f"/api/decks/{deck['id']}/localize"
        first = (await client.post(url, json={"lang": "es"})).json()
        assert first["localized"] == 1 and len(first["unavailable"]) == 2
        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        assert any(c["scryfall_id"] == "sr-es" for c in cards)
        second = (await client.post(url, json={"lang": "es"})).json()
        assert (second["localized"], second["unchanged"]) == (0, 1)
        assert (await client.post(url, json={"lang": "klingon"})).status_code == 400


class TestDeckLifecycle:
    async def test_duplicate(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/duplicate", json={})
        assert r.status_code == 201
        copy = r.json()
        assert copy["name"] == f"{deck['name']} (copia)" and copy["id"] != deck["id"]
        assert sorted(c["name"] for c in copy["cards"]) == sorted(c["name"] for c in deck["cards"])
        [created] = await _events(client, copy["id"])
        assert created["kind"] == "deck_created"
        assert created["payload"]["source"] == "duplicated"
        assert created["payload"]["source_deck_id"] == deck["id"]

        named = await client.post(f"/api/decks/{deck['id']}/duplicate", json={"name": "My Variant"})
        assert named.json()["name"] == "My Variant"
        assert (
            await client.post(f"/api/decks/{deck['id']}/duplicate", json={"name": "  "})
        ).status_code == 400
        assert (await client.post("/api/decks/99999/duplicate", json={})).status_code == 404

    async def test_summary_and_validation_views_are_lightweight(self, client, deck):
        summary = next(d for d in (await client.get("/api/decks/")).json() if d["id"] == deck["id"])
        assert summary["card_count"] == 3 and "cards" not in summary and "validation" not in summary
        validation = (await client.get(f"/api/decks/{deck['id']}/validation")).json()
        assert {
            "format",
            "expected",
            "counted",
            "is_valid",
            "message",
            "level",
            "breakdown",
        } <= set(validation)
        assert "cards" not in validation
        assert (await client.get("/api/decks/99999/validation")).status_code == 404

    async def test_sections(self, client, deck):
        base = f"/api/decks/{deck['id']}"
        for name in ("Sol Ring", "Command Tower"):
            r = await client.patch(
                f"{base}/cards/{_card(deck, name)['id']}", json={"role": "sideboard"}
            )
            assert r.json()["role"] == "sideboard"
        assert (await client.delete(f"{base}/role/sideboard")).json() == {
            "role": "sideboard",
            "deleted": 2,
        }
        assert (await client.delete(f"{base}/role/tokens")).json()["deleted"] == 0
        assert (await client.delete("/api/decks/99999/role/sideboard")).status_code == 404

    async def test_view_survives_a_deck_larger_than_the_sqlite_variable_limit(self, client):
        items = list(range(1201))
        assert [len(c) for c in chunked(items, 500)] == [500, 500, 201]
        assert [i for c in chunked(items, 500) for i in c] == items and list(chunked([], 500)) == []
        async with session_scope() as db:
            deck = Deck(name="cubo", format="commander")
            db.add(deck)
            await db.flush()
            db.add_all(
                DeckCard(
                    deck_id=deck.id,
                    scryfall_id=f"sf-{i}",
                    oracle_id=f"or-{i}",
                    name=f"Carta {i}",
                    quantity=1,
                    role="mainboard",
                    include=True,
                )
                for i in range(1100)
            )
            deck_id = deck.id
        r = await client.get(f"/api/decks/{deck_id}")
        assert r.status_code == 200 and len(r.json()["cards"]) == 1100

    async def test_build_progress(self, client, deck):
        url = f"/api/decks/{deck['id']}/build-progress"
        idle = (await client.get(url)).json()
        assert idle["active"] is False and idle["deck_id"] == deck["id"]
        build_progress.start(deck["id"], total=10, kind="xml")
        build_progress.tick(deck["id"], "Sol Ring")
        build_progress.tick(deck["id"], "Command Tower")
        data = (await client.get(url)).json()
        assert (data["active"], data["total"], data["current"], data["done"], data["percent"]) == (
            True,
            10,
            2,
            False,
            20.0,
        )
        assert data["current_name"] == "Command Tower"
        build_progress.finish(deck["id"])
        assert (await client.get(url)).json()["done"] is True
        build_progress.clear(deck["id"])


class TestGlobalSearch:
    async def test_search_across_decks(self, client, deck):
        await client.post(f"/api/decks/{deck['id']}/duplicate", json={})
        data = (await client.get("/api/decks/_/search-cards?q=sol")).json()
        assert data["groups"][0]["canonical_name"] == "Sol Ring"
        group = data["groups"][0]
        assert group["deck_count"] == 2 and len(group["instances"]) == 2
        inst = next(i for i in group["instances"] if i["deck_id"] == deck["id"])
        assert (inst["role"], inst["quantity"], inst["printing_set"], inst["printing_number"]) == (
            "mainboard",
            1,
            "c21",
            "263",
        )
        assert inst["thumbnail"] is not None

        totals = {
            q: (await client.get(f"/api/decks/_/search-cards?q={q}")).json()["total_groups"]
            for q in ("sol+ring", "SOL+RING", "SoL+RiNg", "s", "xyzzyxxx")
        }
        assert totals["sol+ring"] == totals["SOL+RING"] == totals["SoL+RiNg"] >= 1
        assert totals["s"] == 0 and totals["xyzzyxxx"] == 0


class TestActivityAndUndo:
    async def test_timeline(self, client, deck):
        base = f"/api/decks/{deck['id']}"
        assert (await _events(client, deck["id"]))[-1]["kind"] == "deck_created"
        await client.patch(
            f"{base}/cards/{_card(deck, 'Sol Ring')['id']}", json={"role": "sideboard"}
        )
        await client.patch(
            f"{base}/cards/{_card(deck, 'Command Tower')['id']}", json={"quantity": 3}
        )
        await client.patch(base, json={"name": "Renamed"})

        kinds = [e["kind"] for e in await _events(client, deck["id"])]
        assert kinds[:3] == ["deck_renamed", "card_qty_changed", "card_moved"]
        assert kinds[-1] == "deck_created"
        filtered = (await client.get(f"{base}/activity?kinds=card_moved,card_added")).json()
        assert filtered and all(e["kind"] in {"card_moved", "card_added"} for e in filtered)
        qty = next(e for e in await _events(client, deck["id"]) if e["kind"] == "card_qty_changed")
        assert (qty["payload"]["old_qty"], qty["payload"]["new_qty"]) == (1, 3)
        assert (await client.get("/api/decks/99999/activity")).status_code == 404

        summary = next(
            d
            for d in (await client.get("/api/decks/_/with-activity")).json()
            if d["id"] == deck["id"]
        )
        assert summary["activity_count"] >= 4 and summary["last_activity_kind"] == "deck_renamed"

    async def test_undo_reverts_each_kind_of_change(self, client, deck):
        base = f"/api/decks/{deck['id']}"

        async def undo_last(kind: str) -> dict:
            event_ = next(e for e in await _events(client, deck["id"]) if e["kind"] == kind)
            r = await client.post(f"{base}/activity/{event_['id']}/undo")
            assert r.status_code == 200, (kind, r.text)
            return {"event": event_, "body": r.json()}

        await client.patch(base, json={"name": "New Name"})
        await undo_last("deck_renamed")
        assert (await client.get(base)).json()["name"] == "Test Deck"

        await client.patch(
            f"{base}/cards/{_card(deck, 'Sol Ring')['id']}", json={"role": "sideboard"}
        )
        before = await _events(client, deck["id"])
        moved = await undo_last("card_moved")
        assert "mainboard" in moved["body"]["summary"]
        after = await _events(client, deck["id"])
        assert len(after) == len(before) + 1
        assert after[0]["payload"]["undone_event_id"] == moved["event"]["id"]

        await client.patch(
            f"{base}/cards/{_card(deck, 'Command Tower')['id']}", json={"quantity": 5}
        )
        await undo_last("card_qty_changed")

        r = await client.post(
            f"{base}/cards", json={"name": "Lightning Bolt", "quantity": 1, "role": "mainboard"}
        )
        assert r.status_code == 201
        await undo_last("card_added")

        cards = {c["name"]: c for c in (await client.get(base)).json()["cards"]}
        assert cards["Sol Ring"]["role"] == "mainboard"
        assert cards["Command Tower"]["quantity"] == 1
        assert "Lightning Bolt" not in cards

    async def test_undo_refuses_diverged_or_irreversible_events(self, client, deck):
        kinds = (await client.get("/api/decks/_/undoable-kinds")).json()
        assert {"card_moved", "card_added", "card_art_changed", "deck_renamed"} <= set(kinds)
        assert "deck_created" not in kinds and "role_cleared" not in kinds

        base = f"/api/decks/{deck['id']}"
        signet = _card(deck, "Arcane Signet")
        await client.patch(f"{base}/cards/{signet['id']}", json={"role": "sideboard"})
        first_move = next(e for e in await _events(client, deck["id"]) if e["kind"] == "card_moved")
        await client.patch(f"{base}/cards/{signet['id']}", json={"role": "maybeboard"})
        assert (await client.post(f"{base}/activity/{first_move['id']}/undo")).status_code == 409

        created = next(e for e in await _events(client, deck["id"]) if e["kind"] == "deck_created")
        assert (await client.post(f"{base}/activity/{created['id']}/undo")).status_code == 400


class TestValidationAndPricing:
    def test_legality_rules(self):
        def legal(**fmts):
            return json.dumps(fmts)

        illegal = deck_validation.check_legalities(
            "modern",
            [
                ("Sol Ring", "mainboard", legal(modern="banned"), True),
                ("Lightning Bolt", "mainboard", legal(modern="legal"), True),
                ("Idea", "maybeboard", legal(modern="banned"), True),
                ("Fuera", "mainboard", legal(modern="banned"), False),
                ("Carta Nueva", "mainboard", "", True),
                ("Rota", "mainboard", "{no es json", True),
            ],
        )
        assert [(c.name, c.status) for c in illegal] == [("Sol Ring", "banned")]
        result = deck_validation.validate_deck("modern", [("mainboard", 60, True)], illegal)
        assert (result.is_valid, result.level) == (False, "error") and "no legal" in result.message

        restricted = deck_validation.check_legalities(
            "vintage", [("Black Lotus", "mainboard", legal(vintage="restricted"), True)]
        )
        result = deck_validation.validate_deck("vintage", [("mainboard", 60, True)], restricted)
        assert (result.is_valid, result.level) == (True, "warn")
        clean = deck_validation.validate_deck("modern", [("mainboard", 60, True)], [])
        assert (clean.is_valid, clean.level, clean.illegal) == (True, "ok", [])

    async def test_price_counts_playable_cards_and_flags_unpriced_ones(self, client):
        async with session_scope() as db:
            for sfid, name, eur, usd in (
                ("p-1", "Cara", 10.0, 12.0),
                ("p-2", "Idea", 99.0, 99.0),
                ("p-3", "Sin precio", None, None),
            ):
                db.add(
                    PrintingCache(
                        scryfall_id=sfid,
                        oracle_id=f"o-{sfid}",
                        name=name,
                        set_code="tst",
                        set_name="Test",
                        collector_number=sfid,
                        rarity="rare",
                        price_eur=eur,
                        price_usd=usd,
                    )
                )
            deck = Deck(name="precios", format="commander")
            db.add(deck)
            await db.flush()
            for sfid, name, qty, role in (
                ("p-1", "Cara", 2, "mainboard"),
                ("p-2", "Idea", 1, "maybeboard"),
                ("p-3", "Sin precio", 3, "mainboard"),
            ):
                db.add(
                    DeckCard(
                        deck_id=deck.id,
                        scryfall_id=sfid,
                        oracle_id=f"o-{sfid}",
                        name=name,
                        quantity=qty,
                        role=role,
                        include=True,
                    )
                )
            deck_id = deck.id
        price = (await client.get(f"/api/decks/{deck_id}")).json()["price"]
        assert price == {"eur": 20.0, "usd": 24.0, "priced_cards": 2, "unpriced_cards": 3}


@pytest_asyncio.fixture
async def commander_deck(client, sample_cards):
    async with session_scope() as db:
        for key in ("Sol Ring", "Sol Ring ALT", "Command Tower"):
            await printings.upsert_printing(db, sample_cards[key])
        deck = Deck(name="Mazo Portada", format="commander", commander_scryfall_id="sr-en")
        db.add(deck)
        await db.flush()
        commander = DeckCard(
            deck_id=deck.id,
            oracle_id="oracle-sol-ring",
            name="Sol Ring",
            quantity=1,
            scryfall_id="sr-en",
            role="commander",
        )
        db.add(commander)
        db.add(
            DeckCard(
                deck_id=deck.id,
                oracle_id="oracle-command-tower",
                name="Command Tower",
                quantity=1,
                scryfall_id="ct-en",
                role="mainboard",
            )
        )
        await db.commit()
        return {"deck_id": deck.id, "commander_id": commander.id}


async def _cover_everywhere(client, deck_id: int) -> set[str]:
    deck = (await client.get(f"/api/decks/{deck_id}")).json()
    urls = {
        "editor": next(c for c in deck["cards"] if c["id"] == deck["cover_card_id"])[
            "thumbnail_url"
        ],
        "sidebar": next(
            d
            for d in (await client.get("/api/collection/recent-decks")).json()
            if d["id"] == deck_id
        )["commander_image"],
        "history": next(
            d for d in (await client.get("/api/decks/_/with-activity")).json() if d["id"] == deck_id
        )["commander_image_url"],
    }
    for name, path in (("library", "/decks"), ("landing", "/")):
        html = (await client.get(path, headers=HTML)).text
        urls[name] = (
            html.split(f'data-deck-cover="{deck_id}"')[0].rsplit('src="', 1)[1].split('"', 1)[0]
        )
    editor = (await client.get(f"/decks/{deck_id}", headers=HTML)).text
    urls["header"] = (
        editor.split('class="fx-deck-cover')[0].rsplit('<img src="', 1)[1].split('"', 1)[0]
    )
    return set(urls.values())


async def _add_custom_art(relative_path: str, face: str) -> int:
    async with session_scope() as db:
        ca = CustomArt(
            filename=relative_path.rsplit("/", 1)[-1],
            relative_path=relative_path,
            card_name_normalized="sol ring",
            face=face,
        )
        db.add(ca)
        await db.commit()
        return ca.id


class TestDeckCover:
    async def test_every_view_shows_the_art_the_commander_uses(self, client, commander_deck):
        deck_id, commander_id = commander_deck["deck_id"], commander_deck["commander_id"]
        change_art = f"/api/decks/{deck_id}/cards/change-art"
        assert await _cover_everywhere(client, deck_id) == {"https://x.test/sr-en_n.jpg"}

        back_id = await _add_custom_art("sol ring/b.png", "back")
        r = await client.post(
            change_art,
            json={"deck_card_id": commander_id, "custom_art_id": back_id, "face": "back"},
        )
        assert r.status_code == 200
        assert await _cover_everywhere(client, deck_id) == {"https://x.test/sr-en_n.jpg"}

        r = await client.post(
            change_art,
            json={"deck_card_id": commander_id, "scryfall_id": "sr-alt", "face": "front"},
        )
        assert r.status_code == 200
        assert await _cover_everywhere(client, deck_id) == {"https://x.test/sr-alt_n.jpg"}

        front_id = await _add_custom_art("sol ring/Sol Ring #02.png", "front")
        r = await client.post(
            change_art,
            json={"deck_card_id": commander_id, "custom_art_id": front_id, "face": "front"},
        )
        assert r.status_code == 200
        expected = custom_art.custom_art_url("sol ring/Sol Ring #02.png")
        assert "%23" in expected and await _cover_everywhere(client, deck_id) == {expected}

    async def test_fallbacks_without_a_commander_card(self, client, deck, sample_cards):
        async with session_scope() as db:
            await printings.upsert_printing(db, sample_cards["Sol Ring"])
            legacy = Deck(name="Antiguo", format="commander", commander_scryfall_id="sr-en")
            db.add(legacy)
            await db.commit()
            cover = await deck_covers.cover_for_deck(db, legacy)
            assert (cover.image_url, cover.name, cover.card_id) == (
                "https://x.test/sr-en_n.jpg",
                "Sol Ring",
                None,
            )
            assert (
                await deck_covers.cover_for_deck(db, await db.get(Deck, deck["id"]))
                == deck_covers.EMPTY
            )
        assert (await client.get(f"/api/decks/{deck['id']}")).json()["cover_card_id"] is None

    def test_cover_card_choice_among_partners(self):
        def card(id_, sfid, oracle):
            return DeckCard(
                id=id_,
                deck_id=1,
                oracle_id=oracle,
                name=oracle,
                quantity=1,
                scryfall_id=sfid,
                role="commander",
            )

        a, b = card(1, "a1", "oa"), card(2, "b1", "ob")
        assert (
            pick_cover_card(
                Deck(name="x", format="commander", commander_scryfall_id="b1"), [a, b], {}
            )
            is b
        )
        changed = card(2, "b2", "ob")
        base = PrintingCache(
            scryfall_id="b1",
            oracle_id="ob",
            name="ob",
            set_code="x",
            set_name="X",
            collector_number="1",
            rarity="rare",
        )
        deck = Deck(name="x", format="commander", commander_scryfall_id="b1")
        assert pick_cover_card(deck, [a, changed], {"b1": base}) is changed
        no_commander = Deck(name="x", format="commander", commander_scryfall_id=None)
        assert pick_cover_card(no_commander, [a, b], {}) is a
        assert pick_cover_card(no_commander, [], {}) is None

    async def test_covers_are_resolved_with_a_constant_number_of_queries(
        self, client, sample_cards
    ):
        async with session_scope() as db:
            await printings.upsert_printing(db, sample_cards["Sol Ring"])
            decks = []
            for i in range(25):
                d = Deck(name=f"D{i}", format="commander", commander_scryfall_id="sr-en")
                db.add(d)
                await db.flush()
                db.add(
                    DeckCard(
                        deck_id=d.id,
                        oracle_id="oracle-sol-ring",
                        name="Sol Ring",
                        quantity=1,
                        scryfall_id="sr-en",
                        role="commander",
                    )
                )
                decks.append(d)
            await db.commit()

            statements = []

            def listener(*args, **kwargs):
                statements.append(args[2])

            event.listen(engine.sync_engine, "before_cursor_execute", listener)
            try:
                covers = await deck_covers.covers_for_decks(db, decks)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", listener)
        assert len(covers) == 25 and all(
            c.image_url == "https://x.test/sr-en_n.jpg" for c in covers.values()
        )
        assert len(statements) <= 3, statements


async def _theme(client, deck_id: int, name: str = "T", only_customized: bool = False):
    return await client.post(
        "/api/art-themes",
        json={"deck_id": deck_id, "name": name, "only_customized": only_customized},
    )


class TestArtThemes:
    async def test_create_read_rename_delete(self, client, deck):
        assert (await client.get("/api/art-themes")).json()["themes"] == []
        created = await _theme(client, deck["id"], "Mi estilo retro")
        assert created.status_code == 201
        theme = created.json()
        assert (theme["name"], theme["entry_count"]) == ("Mi estilo retro", 3)
        assert (await _theme(client, deck["id"], "Vacío", only_customized=True)).json()[
            "entry_count"
        ] == 0
        assert (await _theme(client, 999999, "X")).status_code == 404
        assert (await _theme(client, deck["id"], "")).status_code == 422

        body = (await client.get(f"/api/art-themes/{theme['id']}")).json()
        assert len(body["entries"]) == 3 and all(e["oracle_id"] for e in body["entries"])
        assert (await client.get("/api/art-themes/999999")).status_code == 404
        renamed = await client.patch(f"/api/art-themes/{theme['id']}", json={"name": "Después"})
        assert renamed.json()["name"] == "Después"
        assert (await client.delete(f"/api/art-themes/{theme['id']}")).status_code == 204
        assert (await client.get(f"/api/art-themes/{theme['id']}")).status_code == 404

    async def test_preview_and_apply(self, client, deck):
        theme = (await _theme(client, deck["id"])).json()
        before = [
            c["scryfall_id"] for c in (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        ]
        preview = await client.get(f"/api/art-themes/{theme['id']}/preview/{deck['id']}")
        assert preview.status_code == 200 and "would_change" in preview.json()
        after = [
            c["scryfall_id"] for c in (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        ]
        assert before == after

        apply_url = f"/api/art-themes/{theme['id']}/apply"
        applied = (
            await client.post(apply_url, json={"deck_id": deck["id"], "create_snapshot": True})
        ).json()
        assert applied["changed"] == 0 and applied["snapshot"] is not None
        missing = await client.post(apply_url, json={"deck_id": 999999, "create_snapshot": False})
        assert missing.status_code == 404


class TestSnapshots:
    async def test_create_list_and_delete(self, client, deck):
        url = f"/api/decks/{deck['id']}/snapshots"
        assert (await client.get(url)).json()["snapshots"] == []
        created = await client.post(url, json={"label": "Antes del torneo"})
        assert created.status_code == 201 and created.json()["card_count"] == 3
        [listed] = (await client.get(url)).json()["snapshots"]
        assert listed["label"] == "Antes del torneo"
        assert (await client.post(url, json={"label": ""})).json()["label"]
        assert (
            await client.post("/api/decks/999999/snapshots", json={"label": "X"})
        ).status_code == 404

        for snap in (await client.get(url)).json()["snapshots"]:
            assert (await client.delete(f"/api/snapshots/{snap['id']}")).status_code == 204
        assert (await client.get(url)).json()["snapshots"] == []
        assert (await client.post("/api/snapshots/999999/restore")).status_code == 404
        assert (await client.get("/api/snapshots/999999/diff")).status_code == 404
        assert (await client.delete("/api/snapshots/999999")).status_code == 404

    async def test_diff_and_restore(self, client, deck):
        url = f"/api/decks/{deck['id']}/snapshots"
        full = (await client.post(url, json={"label": "A"})).json()
        assert (await client.get(f"/api/snapshots/{full['id']}/diff")).json()["changes"] == []

        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        await client.delete(f"/api/decks/{deck['id']}/cards/{cards[0]['id']}")
        current = (await client.get(f"/api/snapshots/{full['id']}/diff")).json()
        assert current["summary"]["removed"] == 1 and current["to"]["label"] == "Estado actual"
        reduced = (await client.post(url, json={"label": "B"})).json()
        between = (
            await client.get(f"/api/snapshots/{full['id']}/diff?against={reduced['id']}")
        ).json()
        assert between["summary"]["removed"] == 1 and between["to"]["label"] == "B"

        assert (await client.post(f"/api/snapshots/{full['id']}/restore")).status_code == 200
        assert len((await client.get(f"/api/decks/{deck['id']}")).json()["cards"]) == 3
        snapshots = (await client.get(url)).json()["snapshots"]
        assert len(snapshots) == 3 and any(s["auto"] for s in snapshots)
