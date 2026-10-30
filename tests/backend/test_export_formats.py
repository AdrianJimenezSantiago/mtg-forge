from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET

import pytest

from mpc_forge import config as cfg
from mpc_forge.services.system import settings as settings_service


async def _deck_with_sideboard(client) -> int:
    r = await client.post(
        "/api/decks/import/text",
        json={
            "name": "Burn & <Friends>",
            "text": "4 Lightning Bolt\n2 Sol Ring\n\nSideboard\n1 Arcane Signet",
            "format": "modern",
            "include_extras": True,
        },
    )
    deck_id = r.json()["deck"]["id"]
    await client.patch(f"/api/decks/{deck_id}", json={"notes": "Fast & loose"})
    return deck_id


class TestDecklistFormats:
    async def test_mtgo_separates_the_sideboard(self, client):
        deck_id = await _deck_with_sideboard(client)
        r = await client.get(f"/api/decks/{deck_id}/decklist", params={"format": "mtgo"})
        body = r.json()
        assert body["text"] == "4 Lightning Bolt\n2 Sol Ring\n\n1 Arcane Signet\n"
        assert body["total_cards"] == 7 and body["extension"] == "txt"

    async def test_csv_lists_every_board(self, client):
        deck_id = await _deck_with_sideboard(client)
        body = (await client.get(f"/api/decks/{deck_id}/decklist?format=csv")).json()
        rows = list(csv.DictReader(io.StringIO(body["text"])))
        assert {(r["Name"], r["Count"], r["Board"]) for r in rows} == {
            ("Lightning Bolt", "4", "mainboard"),
            ("Sol Ring", "2", "mainboard"),
            ("Arcane Signet", "1", "sideboard"),
        }
        assert body["total_cards"] == 7 and body["filename"].endswith(".csv")

    async def test_cockatrice_is_valid_xml(self, client):
        deck_id = await _deck_with_sideboard(client)
        body = (await client.get(f"/api/decks/{deck_id}/decklist?format=cockatrice")).json()
        root = ET.fromstring(body["text"])  # noqa: S314 (XML generado por la propia app)
        assert root.findtext("deckname") == "Burn & <Friends>"
        assert root.findtext("comments") == "Fast & loose"
        zones = {z.get("name"): {c.get("name"): c.get("number") for c in z} for z in root}
        assert zones["main"] == {"Lightning Bolt": "4", "Sol Ring": "2"}
        assert zones["side"] == {"Arcane Signet": "1"}
        assert body["total_cards"] == 7 and body["filename"].endswith(".cod")

        dl = await client.get(f"/api/decks/{deck_id}/decklist.txt?format=cockatrice")
        assert dl.headers["content-type"].startswith("application/xml")
        assert "filename=" in dl.headers["content-disposition"]

    async def test_text_formats_count_quantities(self, client, deck):
        body = (await client.get(f"/api/decks/{deck['id']}/decklist?format=simple")).json()
        assert body["total_cards"] == 3
        bad = await client.get(f"/api/decks/{deck['id']}/decklist?format=dek")
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "unknown_format"


class TestPdfPresets:
    async def test_save_list_apply_and_delete(self, client):
        assert (await client.get("/api/pdf-presets/")).json() == []
        opts = {"cols": 3, "bleed_mm": 1.5, "page_range": "1-2", "page_size": "a4", "x": [1]}
        r = await client.put("/api/pdf-presets/My printer", json={"opts": opts})
        assert r.status_code == 200
        [preset] = r.json()
        assert preset["name"] == "My printer"
        assert preset["opts"] == {"cols": 3, "bleed_mm": 1.5, "page_size": "a4"}

        await client.put("/api/pdf-presets/my PRINTER", json={"opts": {"cols": 2}})
        listed = (await client.get("/api/pdf-presets/")).json()
        assert [(p["name"], p["opts"]) for p in listed] == [("my PRINTER", {"cols": 2})]

        assert (await client.delete("/api/pdf-presets/My Printer")).status_code == 204
        assert (await client.delete("/api/pdf-presets/My Printer")).status_code == 404

    async def test_invalid_presets(self, client):
        r = await client.put("/api/pdf-presets/x", json={"opts": {"page_range": "1"}})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_preset"
        r = await client.put("/api/pdf-presets/%20", json={"opts": {"cols": 1}})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_preset_name"

    async def test_preset_limit(self, client, monkeypatch):
        from mpc_forge.services.printing import pdf_presets

        monkeypatch.setattr(pdf_presets, "MAX_PRESETS", 2)
        for name in ("a", "b"):
            await client.put(f"/api/pdf-presets/{name}", json={"opts": {"cols": 1}})
        r = await client.put("/api/pdf-presets/c", json={"opts": {"cols": 1}})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "too_many_presets"


class TestMpcTiers:
    def test_validation(self):
        tiers = settings_service.validate_mpc_tiers(
            [
                {"size": 36, "unit_usd": 0.4},
                {"size": "18", "unit_usd": "0.5"},
                {"size": 36, "unit_usd": 0.41},
            ]
        )
        assert tiers == [{"size": 18, "unit_usd": 0.5}, {"size": 36, "unit_usd": 0.41}]
        for bad in ([], [{"size": 0, "unit_usd": 1}], [{"size": 1, "unit_usd": 0}], ["x"], {}):
            with pytest.raises((ValueError, TypeError)):
                settings_service.validate_mpc_tiers(bad)

    async def test_saving_tiers_changes_the_estimate(self, client, deck, monkeypatch):
        monkeypatch.setattr(cfg, "MPC_TIERS", [dict(t) for t in cfg.DEFAULT_MPC_TIERS])
        before = (await client.get(f"/api/decks/{deck['id']}/estimate")).json()
        r = await client.put(
            "/api/settings/", json={"values": {"mpc_tiers": [{"size": 18, "unit_usd": 1.0}]}}
        )
        assert r.status_code == 200 and r.json()["values"]["mpc_tiers"] == [
            {"size": 18, "unit_usd": 1.0}
        ]
        after = (await client.get(f"/api/decks/{deck['id']}/estimate")).json()
        assert after["subtotal_usd"] > before["subtotal_usd"]

        bad = await client.put("/api/settings/", json={"values": {"mpc_tiers": []}})
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "invalid_setting"
        settings_service.apply_to_config({"mpc_tiers": "garbage"})
        assert cfg.MPC_TIERS == cfg.DEFAULT_MPC_TIERS
