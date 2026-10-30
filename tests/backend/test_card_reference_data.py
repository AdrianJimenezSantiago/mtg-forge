from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from mpc_forge.models import DFCPair, IndexedArt, KeyValue
from mpc_forge.services.cards import canonical, dfc_pairs

TRANSFORM = {
    "name": "Delver of Secrets // Insectile Aberration",
    "layout": "transform",
    "card_faces": [{"name": "Delver of Secrets"}, {"name": "Insectile Aberration"}],
}
MDFC = {
    "name": "Valki // Tibalt",
    "layout": "modal_dfc",
    "card_faces": [{"name": "Valki"}, {"name": "Tibalt"}],
}
DIGITAL = {**TRANSFORM, "name": "Digital // Card", "digital": True}
TOKEN = {"layout": "double_faced_token", "card_faces": [{"name": "A"}, {"name": "B"}]}
PARTS = [
    {"id": "bruna", "component": "meld_part", "name": "Bruna"},
    {"id": "gisela", "component": "meld_part", "name": "Gisela"},
    {"id": "brisela", "component": "meld_result", "name": "Brisela"},
]
BRUNA = {"id": "bruna", "name": "Bruna", "oracle_text": "Flying", "all_parts": PARTS}
GISELA = {
    "id": "gisela",
    "name": "Gisela",
    "oracle_text": "Flying\n(Melds with Bruna.)",
    "all_parts": PARTS,
}
BRISELA = {"id": "brisela", "name": "Brisela", "all_parts": PARTS}


class TestPairsFromCards:
    def test_dfc_layouts(self):
        pair = dfc_pairs._dfc_pair_from_card(TRANSFORM)
        assert (pair.front_name, pair.back_name, pair.kind) == (
            "Delver of Secrets",
            "Insectile Aberration",
            "transform",
        )
        assert dfc_pairs._dfc_pair_from_card(TOKEN) is None
        assert dfc_pairs._dfc_pair_from_card({"card_faces": [{"name": "x"}]}) is None
        assert dfc_pairs._dfc_pair_from_card({"card_faces": [{"name": "x"}, {}]}) is None

    def test_meld_halves(self):
        [top] = dfc_pairs._meld_pairs_from_card(BRUNA)
        [bottom] = dfc_pairs._meld_pairs_from_card(GISELA)
        assert (top.back_name, top.kind) == ("Brisela Top", "meld_top")
        assert (bottom.back_name, bottom.kind) == ("Brisela Bottom", "meld_bottom")
        assert dfc_pairs._meld_pairs_from_card(BRISELA) == []
        assert dfc_pairs._meld_pairs_from_card({"all_parts": []}) == []
        no_result = {"id": "x", "all_parts": [{"id": "x", "component": "meld_part"}]}
        assert dfc_pairs._meld_pairs_from_card(no_result) == []


@pytest.fixture
def scryfall_pairs(fake_scryfall):
    async def search_all(query, unique="cards"):
        return (
            [TRANSFORM, MDFC, DIGITAL, TOKEN]
            if query.startswith("is:dfc")
            else [
                BRUNA,
                GISELA,
                BRISELA,
            ]
        )

    fake_scryfall.search_all = AsyncMock(side_effect=search_all)
    return fake_scryfall


class TestSync:
    async def test_sync_lookup_and_ttl(self, client, scryfall_pairs):
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            first = await dfc_pairs.sync_if_stale(db, scryfall_pairs)
            assert first == {"synced": True, "pairs": 4, "reason": "refreshed"}
            again = await dfc_pairs.sync_if_stale(db, scryfall_pairs)
            assert again["reason"] == "up_to_date"
            found = await dfc_pairs.bulk_lookup(db, ["delver of secrets", "Nope", "", "Bruna"])
            assert found == {
                "delver of secrets": {"back_name": "Insectile Aberration", "kind": "transform"},
                "Bruna": {"back_name": "Brisela Top", "kind": "meld_top"},
            }
            assert await dfc_pairs.bulk_lookup(db, []) == {}
            stats = await dfc_pairs.stats(db)
            assert stats["total_pairs"] == 4 and stats["last_synced_at"]

            kv = await db.get(KeyValue, dfc_pairs._LAST_SYNC_KEY)
            kv.value = "2000-01-01T00:00:00"
            await db.commit()
            scryfall_pairs.search_all = AsyncMock(side_effect=RuntimeError("offline"))
            kept = await dfc_pairs.sync_if_stale(db, scryfall_pairs)
            assert kept["synced"] is False and kept["reason"].startswith("error")
            kv.value = "garbage"
            await db.commit()
            assert await dfc_pairs._is_stale(db) is True

    async def test_first_sync_failure_propagates(self, client, fake_scryfall):
        from mpc_forge.db import session_scope

        fake_scryfall.search_all = AsyncMock(side_effect=RuntimeError("offline"))
        async with session_scope() as db:
            with pytest.raises(RuntimeError):
                await dfc_pairs.sync_if_stale(db, fake_scryfall)

    async def test_endpoints(self, client, scryfall_pairs):
        assert (await client.get("/api/dfc-pairs/stats")).json()["total_pairs"] == 0
        r = await client.post("/api/dfc-pairs/sync")
        assert r.status_code in (200, 202)
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            await dfc_pairs.sync_if_stale(db, scryfall_pairs)
        r = await client.get("/api/dfc-pairs/lookup", params={"names": "Valki|Unknown"})
        assert r.status_code == 200
        assert r.json()["total_matched"] == 1 and "Valki" in r.json()["found"]


async def _art(db, source_id: int, name: str, code: str | None, number: str | None) -> IndexedArt:
    from mpc_forge.models import ArtSource

    if await db.get(ArtSource, source_id) is None:
        db.add(ArtSource(id=source_id, name="drive", url=f"https://x.test/{source_id}"))
        await db.flush()
    art = IndexedArt(
        source_id=source_id,
        file_id=f"{name}-{code}-{number}",
        filename=f"{name}.png",
        name_normalized=name.lower(),
        expansion_code=code,
        collector_number=number,
    )
    db.add(art)
    return art


class TestCanonical:
    async def test_tags_are_checked_against_scryfall(self, client, fake_scryfall):
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            assert (await canonical.validate_and_enrich(db, fake_scryfall))["checked"] == 0
            await _art(db, 1, "Sol Ring", "c21", "263")
            await _art(db, 1, "Bolt", "lea", "100")
            await _art(db, 1, "Fake", "zzz", "1")
            await _art(db, 2, "Other", "zzz", "2")
            await db.commit()
            report = await canonical.validate_and_enrich(db, fake_scryfall, source_id=1)
            assert report == {"checked": 3, "valid": 2, "invalid": 1, "cache_hits": 0}
            fake = (
                await db.scalars(select(IndexedArt).where(IndexedArt.filename == "Fake.png"))
            ).one()
            assert fake.expansion_code is None and fake.canonical_source == "invalid"

            again = await canonical.validate_and_enrich(db, fake_scryfall)
            assert again["cache_hits"] == 2 and again["invalid"] == 1

            details = await canonical.get_canonical_details(db, "C21", "263")
            assert details["name"] == "Sol Ring" and details["scryfall_id"] == "sr-en"
            assert await canonical.get_canonical_details(db, "xxx", "1") is None

    async def test_scryfall_failures_skip_the_batch(self, client, fake_scryfall):
        from mpc_forge.db import session_scope

        fake_scryfall.collection = AsyncMock(side_effect=RuntimeError("offline"))
        async with session_scope() as db:
            await _art(db, 1, "Fake", "zzz", "9")
            await db.commit()
            report = await canonical.validate_and_enrich(db, fake_scryfall)
        assert report == {"checked": 1, "valid": 0, "invalid": 0, "cache_hits": 0}


async def test_pairs_table_is_replaced_on_sync(client, scryfall_pairs):
    from mpc_forge.db import session_scope

    async with session_scope() as db:
        db.add(DFCPair(front_name="Old", back_name="Stale", kind="transform"))
        await db.commit()
        await dfc_pairs.sync_if_stale(db, scryfall_pairs)
        names = set((await db.scalars(select(DFCPair.front_name))).all())
    assert "Old" not in names and "Valki" in names
