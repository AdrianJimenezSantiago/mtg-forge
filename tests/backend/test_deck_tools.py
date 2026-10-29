from __future__ import annotations

import pytest

from mpc_forge.services.decks import deck_tools


async def _cards(client, deck_id: int) -> dict[str, dict]:
    deck = (await client.get(f"/api/decks/{deck_id}")).json()
    return {c["name"]: c for c in deck["cards"]}


class TestBulkEdit:
    async def test_move_exclude_include_and_delete(self, client, deck):
        cards = await _cards(client, deck["id"])
        ids = [cards["Sol Ring"]["id"], cards["Arcane Signet"]["id"]]
        url = f"/api/decks/{deck['id']}/cards/bulk"

        r = await client.post(url, json={"card_ids": ids, "action": "move", "role": "sideboard"})
        assert r.status_code == 200 and r.json() == {"changed": 2}
        again = await client.post(
            url, json={"card_ids": ids, "action": "move", "role": "sideboard"}
        )
        assert again.json() == {"changed": 0}

        r = await client.post(url, json={"card_ids": ids, "action": "exclude"})
        assert r.json() == {"changed": 2}
        cards = await _cards(client, deck["id"])
        assert cards["Sol Ring"]["role"] == "sideboard" and cards["Sol Ring"]["include"] is False

        r = await client.post(url, json={"card_ids": ids[:1], "action": "include"})
        assert r.json() == {"changed": 1}

        r = await client.post(url, json={"card_ids": ids, "action": "delete"})
        assert r.json() == {"changed": 2}
        assert set(await _cards(client, deck["id"])) == {"Command Tower"}

        kinds = [e["kind"] for e in (await client.get(f"/api/decks/{deck['id']}/activity")).json()]
        assert kinds.count("card_removed") == 2 and kinds.count("card_moved") == 2

    async def test_each_bulk_change_can_be_undone(self, client, deck):
        cards = await _cards(client, deck["id"])
        url = f"/api/decks/{deck['id']}/cards/bulk"
        await client.post(
            url,
            json={"card_ids": [cards["Sol Ring"]["id"]], "action": "move", "role": "maybeboard"},
        )
        event = (await client.get(f"/api/decks/{deck['id']}/activity")).json()[0]
        assert event["kind"] == "card_moved"
        r = await client.post(f"/api/decks/{deck['id']}/activity/{event['id']}/undo")
        assert r.status_code == 200
        assert (await _cards(client, deck["id"]))["Sol Ring"]["role"] == "mainboard"

    async def test_invalid_requests(self, client, deck):
        url = f"/api/decks/{deck['id']}/cards/bulk"
        bad_role = await client.post(url, json={"card_ids": [1], "action": "move", "role": "attic"})
        assert bad_role.status_code == 400 and bad_role.json()["detail"]["code"] == "invalid_role"
        assert (
            await client.post(url, json={"card_ids": [], "action": "delete"})
        ).status_code == 422
        missing = await client.post(
            "/api/decks/999/cards/bulk", json={"card_ids": [1], "action": "delete"}
        )
        assert missing.status_code == 404

    async def test_cards_of_other_decks_are_ignored(self, client, deck):
        other = await client.post(
            "/api/decks/import/text", json={"name": "Other", "text": "1 Sol Ring"}
        )
        other_card = other.json()["deck"]["cards"][0]["id"]
        r = await client.post(
            f"/api/decks/{deck['id']}/cards/bulk",
            json={"card_ids": [other_card], "action": "delete"},
        )
        assert r.json() == {"changed": 0}


class TestCompare:
    async def test_differences_by_card(self, client, deck):
        other = await client.post(
            "/api/decks/import/text",
            json={"name": "Other", "text": "2 Sol Ring\n1 Lightning Bolt\n1 Command Tower"},
        )
        other_id = other.json()["deck"]["id"]
        r = await client.get(f"/api/decks/{deck['id']}/compare/{other_id}")
        assert r.status_code == 200
        body = r.json()
        assert [c["name"] for c in body["only_a"]] == ["Arcane Signet"]
        assert [c["name"] for c in body["only_b"]] == ["Lightning Bolt"]
        assert body["different"] == [{"name": "Sol Ring", "a": 1, "b": 2, "key": "oracle-sol-ring"}]
        assert body["shared"] == 2 and body["shared_cards"] == 2
        assert (await client.get(f"/api/decks/{deck['id']}/compare/999")).status_code == 404


class _FakeSite:
    name = "FakeSite"
    text = ""

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        return cls.text


@pytest.fixture
def fake_site(monkeypatch):
    monkeypatch.setattr(deck_tools, "resolve_site", lambda url: _FakeSite)
    return _FakeSite


class TestSync:
    async def _deck_with_source(self, client, deck):
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck

        async with session_scope() as db:
            row = await db.get(Deck, deck["id"])
            row.source_url = "https://fake.test/decks/1"

    async def test_preview_and_apply_keep_the_art_of_unchanged_cards(self, client, deck, fake_site):
        await self._deck_with_source(client, deck)
        cards = await _cards(client, deck["id"])
        await client.post(
            f"/api/decks/{deck['id']}/cards/change-art",
            json={"deck_card_id": cards["Sol Ring"]["id"], "scryfall_id": "sr-alt"},
        )
        fake_site.text = "3 Sol Ring\n1 Lightning Bolt\n1 Command Tower\n1 Nonexistent Card"

        preview = await client.get(f"/api/decks/{deck['id']}/sync/preview")
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["site"] == "FakeSite" and body["roles"] == ["commander", "mainboard"]
        changes = {c["name"]: (c["change"], c["before"], c["after"]) for c in body["changes"]}
        assert changes == {
            "Lightning Bolt": ("added", 0, 1),
            "Arcane Signet": ("removed", 1, 0),
            "Sol Ring": ("quantity", 1, 3),
        }
        assert [u["name"] for u in body["unresolved"]] == ["Nonexistent Card"]

        keep_signet = [c["key"] for c in body["changes"] if c["name"] != "Arcane Signet"]
        r = await client.post(f"/api/decks/{deck['id']}/sync/apply", json={"keys": keep_signet})
        assert r.status_code == 200, r.text
        assert r.json()["applied"] == 2 and r.json()["snapshot_id"]

        cards = await _cards(client, deck["id"])
        assert cards["Sol Ring"]["quantity"] == 3 and cards["Sol Ring"]["scryfall_id"] == "sr-alt"
        assert "Lightning Bolt" in cards and "Arcane Signet" in cards
        kinds = [e["kind"] for e in (await client.get(f"/api/decks/{deck['id']}/activity")).json()]
        assert kinds[0] == "deck_synced"

        again = await client.post(f"/api/decks/{deck['id']}/sync/apply", json={})
        assert again.json()["applied"] == 1
        assert "Arcane Signet" not in await _cards(client, deck["id"])
        nothing = await client.post(f"/api/decks/{deck['id']}/sync/apply", json={})
        assert nothing.json() == {
            "applied": 0,
            "added": 0,
            "removed": 0,
            "quantity": 0,
            "snapshot_id": None,
        }

    async def test_decks_without_a_source_cannot_sync(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/sync/preview")
        assert r.status_code == 400 and r.json()["detail"]["code"] == "no_source_url"

    async def test_site_errors_are_reported(self, client, deck, fake_site, monkeypatch):
        from mpc_forge.clients.import_sites.base import ImportSiteError

        await self._deck_with_source(client, deck)

        async def boom(url):
            raise ImportSiteError("down")

        monkeypatch.setattr(fake_site, "retrieve_card_list", boom)
        r = await client.get(f"/api/decks/{deck['id']}/sync/preview")
        assert r.status_code == 502 and r.json()["detail"]["code"] == "import_failed"
