"""Tests de la vista de Ajustes rediseñada + ssl_insecure runtime."""
from __future__ import annotations


class TestSettingsRedesign:
    async def test_settings_endpoint_returns_new_groups(self, client):
        r = await client.get("/api/settings/")
        assert r.status_code == 200
        data = r.json()
        groups = sorted({d["group"] for d in data["definitions"]})
        assert groups == ["General", "MPC Autofill", "Precios y envío", "Red y conexión"]

    async def test_ssl_insecure_setting_exists(self, client):
        r = await client.get("/api/settings/")
        defs = r.json()["definitions"]
        ssl_def = next(d for d in defs if d["key"] == "ssl_insecure")
        assert ssl_def["type"] == "bool"
        assert ssl_def["group"] == "Red y conexión"

    async def test_preferred_language_in_general(self, client):
        r = await client.get("/api/settings/")
        defs = r.json()["definitions"]
        by_group = {}
        for d in defs:
            by_group.setdefault(d["group"], []).append(d["key"])
        assert "preferred_language" in by_group["General"]
        assert "prefer_full_art" in by_group["General"]
        assert "default_cardstock" in by_group["General"]

    async def test_ssl_insecure_toggle_propagates_to_runtime(self, client):
        from mpc_forge import ssl_config

        # Estado inicial: False
        assert ssl_config.ssl_insecure() is False

        r = await client.put("/api/settings/", json={"values": {"ssl_insecure": True}})
        assert r.status_code == 200
        assert ssl_config.ssl_insecure() is True

        r = await client.put("/api/settings/", json={"values": {"ssl_insecure": False}})
        assert r.status_code == 200
        assert ssl_config.ssl_insecure() is False


class TestSettingsHTML:
    """Verificaciones básicas sobre el HTML — que no rompan silenciosamente
    los pilares del rediseño."""

    async def test_settings_page_renders(self, client):
        r = await client.get("/settings")
        assert r.status_code == 200
        html = r.text
        # Pilares del rediseño
        for needle in [
            'sticky top-0',           # header sticky
            'x-model="searchQuery"',   # buscador global
            'showDrivesModal',         # modal de drives
            'Red y conexión',          # sección
            'MPC Autofill',            # sección
            'ssl_insecure',            # setting
            'resetAll',                # botón restablecer
        ]:
            assert needle in html, f"Falta {needle!r} en settings.html"
