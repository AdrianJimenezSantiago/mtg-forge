from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

from mpc_forge import config as cfg
from mpc_forge.services.printing import xml_generator as xg
from mpc_forge.services.printing.xml_generator import DeckCardResolved

from ..conftest import _card_stub


def _parse(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text(encoding="utf-8"))  # noqa: S314 (XML de la propia app)


def _cards(root: ET.Element, zone: str) -> list[dict[str, str]]:
    return [
        {child.tag: child.text or "" for child in card} for card in root.findall(f"{zone}/card")
    ]


class TestBuildXml:
    def test_slots_backs_and_cardback(self, tmp_path):
        cards = [
            DeckCardResolved("Sol Ring", 2, "sr", tmp_path / "sr.png", query="sol ring"),
            DeckCardResolved(
                "Delver of Secrets // Insectile Aberration",
                1,
                "dv",
                tmp_path / "dv.png",
                back_path=tmp_path / "dv-back.png",
                back_name="Insectile Aberration",
                query="delver",
            ),
        ]
        out = tmp_path / "out" / "deck.xml"
        result = xg.build_xml(cards, out, "(S30) Standard Smooth", True, tmp_path / "Back.png")

        assert result.total_cards == 3
        assert result.fronts_by_slot == [
            "Sol Ring",
            "Sol Ring",
            "Delver of Secrets // Insectile Aberration",
        ]
        root = _parse(out)
        assert root.findtext("details/quantity") == "3"
        assert root.findtext("details/stock") == "(S30) Standard Smooth"
        assert root.findtext("details/foil") == "true"
        fronts = _cards(root, "fronts")
        assert [(f["slots"], f["name"]) for f in fronts] == [
            ("0,1", "Sol Ring"),
            ("2", "Delver of Secrets // Insectile Aberration"),
        ]
        assert fronts[0]["id"] == str(tmp_path / "sr.png")
        backs = _cards(root, "backs")
        assert backs[0] == {
            "id": str(tmp_path / "dv-back.png"),
            "slots": "2",
            "name": "Insectile Aberration",
            "query": "insectile aberration",
        }
        assert backs[1]["slots"] == "0,1" and backs[1]["name"] == "Back.png"
        assert root.findtext("cardback") == str(tmp_path / "Back.png")

    def test_web_mode_leaves_ids_empty_and_no_cardback_skips_the_slot(self, tmp_path):
        cards = [DeckCardResolved("Opt", 1, "o", tmp_path / "o.png")]
        out = tmp_path / "web.xml"
        xg.build_xml(cards, out, "(M31) Linen", False, None, web_mode=True)
        root = _parse(out)
        assert root.findtext("details/foil") == "false"
        assert _cards(root, "fronts")[0]["id"] == ""
        assert _cards(root, "backs") == [] and root.find("cardback") is None

        xg.build_xml(cards, out, "(M31) Linen", False, tmp_path / "cb.png", web_mode=True)
        root = _parse(out)
        assert _cards(root, "backs")[0]["id"] == "" and root.find("cardback") is None

    def test_cardback_is_not_added_when_every_slot_has_its_own_back(self, tmp_path):
        cards = [DeckCardResolved("A", 1, "a", tmp_path / "a", back_path=tmp_path / "b")]
        out = tmp_path / "x.xml"
        xg.build_xml(cards, out, "s", False, tmp_path / "cb.png")
        assert len(_cards(_parse(out), "backs")) == 1

    def test_slug_and_meld_lookup(self):
        assert xg._slug("Fire // Ice - Foo!") == "fire ice foo"
        part = {"component": "meld_result", "id": "m-1"}
        assert xg._meld_result_id(SimpleNamespace(related_parts=json.dumps([part]))) == "m-1"
        assert xg._meld_result_id(SimpleNamespace(related_parts="")) is None
        assert xg._meld_result_id(SimpleNamespace(related_parts="{bad")) is None
        other = [{"component": "token", "id": "t"}]
        assert xg._meld_result_id(SimpleNamespace(related_parts=json.dumps(other))) is None

    def test_default_cardback(self, tmp_path, monkeypatch):
        import dataclasses

        monkeypatch.setattr(cfg, "PATHS", dataclasses.replace(cfg.PATHS, cardbacks_dir=tmp_path))
        assert xg.default_cardback_path() is None
        (tmp_path / f"{cfg.DEFAULT_CARDBACK_NAME}.jpg").write_bytes(b"x")
        assert (
            xg.default_cardback_path() == (tmp_path / f"{cfg.DEFAULT_CARDBACK_NAME}.jpg").resolve()
        )


class _FakeArtCache:
    def __init__(self, missing: set[tuple[str, str]] | None = None) -> None:
        self.requests: list[tuple[str, str]] = []
        self.missing = missing or set()

    async def ensure_many(self, db, requests, **_):
        self.requests = list(requests)
        return {
            req: SimpleNamespace(relative_path=f"{req[0]}-{req[1]}.png")
            for req in requests
            if req not in self.missing
        }

    def absolute_path(self, art):
        return cfg.PATHS.art_dir / art.relative_path


DFC = _card_stub(
    "Delver of Secrets // Insectile Aberration",
    "dv-en",
    "51",
    set_code="isd",
    layout="transform",
    card_faces=[
        {"name": "Delver of Secrets", "image_uris": {"normal": "https://x.test/f.jpg"}},
        {"name": "Insectile Aberration", "image_uris": {"normal": "https://x.test/b.jpg"}},
    ],
)
MELD = _card_stub(
    "Bruna, the Fading Light",
    "bruna",
    "15",
    set_code="emn",
    layout="meld",
    all_parts=[{"component": "meld_result", "id": "brisela", "name": "Brisela"}],
)
MELD_RESULT = _card_stub("Brisela, Voice of Nightmares", "brisela", "15b", set_code="emn")


async def _add(db, deck_id: int, card: dict, **extra):
    from mpc_forge.models import DeckCard

    db.add(
        DeckCard(
            deck_id=deck_id,
            oracle_id=card["oracle_id"],
            name=card["name"],
            quantity=extra.pop("quantity", 1),
            scryfall_id=card["id"],
            role="mainboard",
            include=extra.pop("include", True),
            **extra,
        )
    )


class TestResolveDeck:
    async def test_fronts_dfc_backs_meld_backs_and_custom_art(self, client, deck, fake_scryfall):
        from mpc_forge.db import session_scope
        from mpc_forge.models import CustomArt, Deck
        from mpc_forge.services.cards.printings import upsert_printings

        custom_dir = cfg.PATHS.custom_art_dir
        (custom_dir / "Sol Ring - Mine.png").write_bytes(b"png")
        async with session_scope() as db:
            await upsert_printings(db, [DFC, MELD, MELD_RESULT])
            mine = CustomArt(
                filename="Sol Ring - Mine.png",
                relative_path="Sol Ring - Mine.png",
                card_name_normalized="sol ring",
            )
            gone = CustomArt(
                filename="Gone.png", relative_path="Gone.png", card_name_normalized="arcane signet"
            )
            db.add_all([mine, gone])
            await db.flush()
            from sqlalchemy import select

            from mpc_forge.models import DeckCard

            cards = {
                c.name: c
                for c in (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck["id"])))
            }
            cards["Sol Ring"].custom_art_front_id = mine.id
            cards["Arcane Signet"].custom_art_front_id = gone.id
            await _add(db, deck["id"], DFC, quantity=2)
            await _add(db, deck["id"], MELD)
            await _add(db, deck["id"], MELD_RESULT, include=False)

        cache = _FakeArtCache()
        progress: list[str] = []
        try:
            async with session_scope() as db:
                deck_row = await db.get(Deck, deck["id"])
                resolved = await xg.resolve_deck_for_xml(
                    db, fake_scryfall, cache, deck_row, on_progress=progress.append
                )
        finally:
            (custom_dir / "Sol Ring - Mine.png").unlink()

        by_name = {r.name: r for r in resolved}
        assert by_name["Sol Ring"].front_path == custom_dir / "Sol Ring - Mine.png"
        assert by_name["Arcane Signet"].front_path.name == "as-en-front.png"
        delver = by_name[DFC["name"]]
        assert delver.quantity == 2 and delver.back_path.name == "dv-en-back.png"
        assert delver.query == "delver of secrets insectile aberration"
        bruna = by_name[MELD["name"]]
        assert bruna.back_path.name == "brisela-front.png"
        assert bruna.back_name == "Brisela, Voice of Nightmares (meld)"
        assert MELD_RESULT["name"] not in by_name
        assert ("sr-en", "front") not in cache.requests
        assert set(progress) == set(by_name)

    async def test_cards_without_art_are_left_out(self, client, deck, fake_scryfall):
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck

        cache = _FakeArtCache(missing={("sr-en", "front")})
        async with session_scope() as db:
            resolved = await xg.resolve_deck_for_xml(
                db,
                fake_scryfall,
                cache,
                await db.get(Deck, deck["id"]),
                on_progress=lambda n: 1 / 0,
            )
        assert "Sol Ring" not in {r.name for r in resolved} and len(resolved) == 2

    async def test_empty_deck_and_slot_plan(self, client, deck):
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck

        async with session_scope() as db:
            empty = Deck(name="Empty")
            db.add(empty)
            await db.flush()
            assert await xg.resolve_deck_for_xml(db, None, _FakeArtCache(), empty) == []
            plan = await xg.plan_deck_slots(db, await db.get(Deck, deck["id"]))
        assert sorted(p.name for p in plan) == ["Arcane Signet", "Command Tower", "Sol Ring"]
        assert all(p.back_path is None and p.front_path == Path("") for p in plan)
