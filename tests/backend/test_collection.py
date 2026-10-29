from __future__ import annotations

import csv
import io

import pytest

from mpc_forge.services.cards import collection_io

MOXFIELD = (
    '"Count","Tradelist Count","Name","Edition","Condition","Language","Foil","Collector Number"\n'
    '"2","0","Sol Ring","c21","Near Mint","English","","263"\n'
    '"1","0","Sol Ring","c21","Near Mint","English","foil","263"\n'
    '"3","0","Lightning Bolt","lea","Near Mint","English","","100"\n'
)
MANABOX = (
    "Name,Set code,Set name,Collector number,Foil,Rarity,Quantity,Scryfall ID\n"
    "Command Tower,c21,Commander 2021,347,normal,common,4,ct-en\n"
)
DECKBOX = "Count;Name;Edition;Card Number;Foil\n1;Arcane Signet;Commander 2021;234;\n"


class TestParsing:
    def test_columns_from_the_usual_sites(self):
        mox = collection_io.parse_csv(MOXFIELD)
        assert [(r.name, r.quantity, r.foil, r.set, r.number) for r in mox] == [
            ("Sol Ring", 2, False, "c21", "263"),
            ("Sol Ring", 1, True, "c21", "263"),
            ("Lightning Bolt", 3, False, "lea", "100"),
        ]
        mana = collection_io.parse_csv(MANABOX)[0]
        assert (mana.quantity, mana.scryfall_id, mana.foil) == (4, "ct-en", False)
        deckbox = collection_io.parse_csv(DECKBOX)[0]
        assert deckbox.set is None and deckbox.name == "Arcane Signet"

    def test_bad_rows_are_skipped_and_limits_apply(self):
        rows = collection_io.parse_csv("Name,Count\n,3\nSol Ring,0\nSol Ring,x\nSol Ring,99999\n")
        assert [(r.name, r.quantity) for r in rows] == [("Sol Ring", collection_io.MAX_QUANTITY)]
        assert collection_io.parse_csv("Name\nSol Ring\n")[0].quantity == 1

    def test_files_without_a_name_column_are_rejected(self):
        with pytest.raises(collection_io.CollectionImportError) as exc:
            collection_io.parse_csv("foo,bar\n1,2\n")
        assert exc.value.code == "collection_csv_columns"


async def _import(client, text: str, mode: str = "merge"):
    return await client.post(
        "/api/collection/import",
        files={"file": ("c.csv", text.encode(), "text/csv")},
        data={"mode": mode},
    )


class TestImportExport:
    async def test_merge_then_replace(self, client):
        r = await _import(client, MOXFIELD + "1,0,Nonexistent Card,xxx,,,,1\n")
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["rows"] == 4 and rep["cards"] == 6 and rep["printings"] == 2
        assert rep["added"] == 2 and [u["name"] for u in rep["unresolved"]] == ["Nonexistent Card"]

        stats = (await client.get("/api/collection/stats")).json()
        assert stats == {"total_owned": 2, "unique_sets": 2, "total_copies": 6, "foil_copies": 1}

        again = (await _import(client, MOXFIELD)).json()
        assert again["updated"] == 2 and again["added"] == 0
        assert (await client.get("/api/collection/stats")).json()["total_copies"] == 12

        replaced = (await _import(client, MANABOX, mode="replace")).json()
        assert replaced["removed"] == 2 and replaced["added"] == 1
        stats = (await client.get("/api/collection/stats")).json()
        assert stats["total_owned"] == 1 and stats["total_copies"] == 4

    async def test_export_round_trips(self, client):
        await _import(client, MOXFIELD)
        r = await client.get("/api/collection/export.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0] == list(collection_io.EXPORT_HEADER)
        assert ["2", "Sol Ring", "c21", "263", "", "sr-en"] in rows
        assert ["1", "Sol Ring", "c21", "263", "foil", "sr-en"] in rows
        parsed = collection_io.parse_csv(r.text)
        assert sum(p.quantity for p in parsed) == 6

    async def test_rejections(self, client):
        bad = await _import(client, "foo,bar\n1,2\n")
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "collection_csv_columns"
        mode = await _import(client, MOXFIELD, mode="wipe")
        assert mode.status_code == 422


CARD = {
    "scryfall_id": "sr-en",
    "oracle_id": "oracle-sol-ring",
    "name": "Sol Ring",
    "set_code": "c21",
    "set_name": "C21",
    "collector_number": "263",
}


class TestQuantities:
    async def test_set_quantity_and_set_listing(self, client, fake_scryfall, sample_cards):
        from unittest.mock import AsyncMock

        fake_scryfall.search_all = AsyncMock(
            return_value=[c for c in sample_cards.values() if c["set"] == "c21"]
        )
        r = await client.post(
            "/api/collection/set-quantity", json={**CARD, "quantity": 3, "foil_quantity": 1}
        )
        assert r.json() == {"owned": True, "quantity": 3, "foil_quantity": 1, "set_owned_count": 1}
        cards = (await client.get("/api/collection/sets/c21/cards")).json()
        sol = next(c for c in cards if c["scryfall_id"] == "sr-en")
        assert sol["owned"] and (sol["quantity"], sol["foil_quantity"]) == (3, 1)

        gone = await client.post(
            "/api/collection/set-quantity", json={**CARD, "quantity": 0, "foil_quantity": 0}
        )
        assert gone.json()["owned"] is False
        assert (await client.get("/api/collection/stats")).json()["total_owned"] == 0
        assert (
            await client.post("/api/collection/set-quantity", json={**CARD, "quantity": -1})
        ).status_code == 422

    async def test_toggled_cards_count_as_one_copy(self, client):
        await client.post("/api/collection/toggle-owned", json=CARD)
        assert (await client.get("/api/collection/stats")).json()["total_copies"] == 1

    async def test_print_needs_use_the_quantities(self, client):
        r = await client.post(
            "/api/decks/import/text",
            json={"name": "Rings", "text": "3 Sol Ring\n1 Command Tower", "format": "modern"},
        )
        deck_id = r.json()["deck"]["id"]
        await client.post(
            "/api/collection/set-quantity", json={**CARD, "quantity": 1, "foil_quantity": 1}
        )
        needs = (await client.get(f"/api/decks/{deck_id}/print-needs")).json()
        sol = next(c for c in needs["cards"] if c["name"] == "Sol Ring")
        assert (sol["quantity"], sol["owned"], sol["needed"]) == (3, 2, 1)
