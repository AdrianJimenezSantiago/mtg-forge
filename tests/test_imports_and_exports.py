"""Tests de imports (Moxfield + texto), unresolved, decklist export y localización."""
from __future__ import annotations


class TestImportUnresolved:
    """Feature: reporte de cartas no importadas."""

    async def test_import_text_reports_unresolved(self, client):
        r = await client.post("/api/decks/import/text", json={
            "name": "With Bad Card",
            "text": "1 Sol Ring\n1 Command Tower\n1 Not A Real Card",
            "format": "commander",
        })
        assert r.status_code == 200
        result = r.json()
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0]["name"] == "Not A Real Card"
        assert result["unresolved"][0]["raw_line"] == "1 Not A Real Card"
        assert result["resolved_count"] == 2
        assert result["total_entries"] == 3

    async def test_import_all_resolved_returns_empty_unresolved(self, client):
        r = await client.post("/api/decks/import/text", json={
            "name": "All Good",
            "text": "1 Sol Ring\n1 Command Tower",
            "format": "commander",
        })
        assert r.status_code == 200
        assert r.json()["unresolved"] == []


class TestDecklistExport:
    """Feature: exportar decklist como string."""

    async def test_export_simple_format(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/decklist?format=simple")
        assert r.status_code == 200
        text = r.json()["text"]
        # Formato simple: solo "N Nombre" por línea
        assert "1 Sol Ring" in text
        assert "1 Command Tower" in text
        # No debe incluir códigos de set
        assert "(c21)" not in text
        assert "(C21)" not in text

    async def test_export_with_set_format(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/decklist?format=with_set")
        text = r.json()["text"]
        assert "1 Sol Ring (c21) 263" in text

    async def test_export_arena_format_uppercase_set(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/decklist?format=arena")
        text = r.json()["text"]
        assert "1 Sol Ring (C21) 263" in text

    async def test_invalid_format_returns_400(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/decklist?format=nonsense")
        assert r.status_code == 400

    async def test_download_txt_endpoint(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/decklist.txt?format=with_set")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "Sol Ring" in r.text


class TestLocalization:
    """Feature: idioma del arte de las cartas."""

    async def test_supported_langs_lists_common_languages(self, client):
        r = await client.get("/api/decks/_/supported-langs")
        assert r.status_code == 200
        langs = r.json()
        assert langs.get("es") == "Español"
        assert langs.get("en") == "English"
        assert langs.get("ja") == "日本語"

    async def test_localize_to_spanish_updates_card(self, client, deck):
        # Solo Sol Ring tiene versión ES en SAMPLE_CARDS
        r = await client.post(f"/api/decks/{deck['id']}/localize", json={"lang": "es"})
        assert r.status_code == 200
        result = r.json()
        assert result["localized"] == 1
        # Command Tower y Arcane Signet no tienen ES → van a unavailable
        assert len(result["unavailable"]) == 2

        # Verificamos que Sol Ring apunta ahora al printing ES
        deck_after = (await client.get(f"/api/decks/{deck['id']}")).json()
        sol = next(c for c in deck_after["cards"] if "Sol Ring" in c["name"] or "Anillo" in c["name"])
        assert sol["scryfall_id"] == "sr-es"

    async def test_localize_idempotent(self, client, deck):
        await client.post(f"/api/decks/{deck['id']}/localize", json={"lang": "es"})
        # Segunda llamada: ya está localizado, unchanged=1
        r = await client.post(f"/api/decks/{deck['id']}/localize", json={"lang": "es"})
        result = r.json()
        assert result["localized"] == 0
        assert result["unchanged"] == 1

    async def test_localize_invalid_lang_returns_400(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/localize", json={"lang": "klingon"})
        assert r.status_code == 400
