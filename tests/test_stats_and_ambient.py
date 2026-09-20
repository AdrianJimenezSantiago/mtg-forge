"""Modal de estadísticas (gráficas propias) y fondo ambiental.

El modal ya no usa Chart.js: sus gráficas son HTML/SVG animadas con CSS.
Varias traducciones se componen en tiempo de ejecución
(``window._t('stats_color_' + key)``), así que el test genérico de claves no
las ve; aquí se comprueban las familias completas contra los valores que
produce ``computeStats`` en deck-editor.js.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = {"accept": "text/html"}


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestStatsModal:
    def test_no_chart_js_left_in_the_editor(self):
        js = _read("static/js/deck-editor.js")
        assert "new Chart(" not in js
        assert "chart.umd.js" not in js
        assert "_chartInstances" not in js
        assert "<canvas" not in _read("templates/deck.html")

    def test_editor_links_the_stats_stylesheet(self):
        html = _read("templates/deck.html")
        assert "/static/stats.css?v={{ asset_v('stats.css') }}" in html

    async def test_modal_renders_translated(self, client, deck):
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert r.status_code == 200
        assert "Estadísticas del mazo" in r.text
        client.cookies.set("lang", "en")
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert "Deck statistics" in r.text
        assert "Curva de maná" not in r.text

    def test_svg_segments_are_not_built_with_x_for(self):
        """`x-for` no funciona dentro de <svg> (allí <template> no tiene
        `.content`): los arcos se generan con Jinja."""
        html = _read("templates/deck.html")
        start = html.index("MODAL DE ESTADÍSTICAS")
        for svg in re.findall(r"<svg.*?</svg>", html[start:], flags=re.S):
            assert "x-for" not in svg

    @pytest.mark.parametrize("family,keys", [
        ("stats_color_", ["W", "U", "B", "R", "G", "M", "C"]),
        ("stats_type_", ["Creature", "Instant", "Sorcery", "Enchantment", "Artifact",
                         "Planeswalker", "Battle", "Land", "Other"]),
        ("stats_rarity_", ["common", "uncommon", "rare", "mythic", "special", "bonus", "unknown"]),
    ])
    def test_dynamic_key_families_are_complete(self, family, keys):
        from mpc_forge.services.i18n import _TRANSLATIONS

        for lang, table in _TRANSLATIONS.items():
            missing = [k for k in keys if family + k not in table]
            assert not missing, f"{lang}: faltan {family}{missing}"

    def test_js_produces_only_known_keys(self):
        """Las listas de deck-editor.js y las familias traducidas coinciden."""
        js = _read("static/js/deck-editor.js")
        colors = re.search(r"const CURVE_COLORS = \[([^\]]+)\]", js).group(1)
        assert re.findall(r"'(\w)'", colors) == ["W", "U", "B", "R", "G", "M", "C"]
        rarity = re.search(r"const RARITY_ORDER = \[([^\]]+)\]", js).group(1)
        assert set(re.findall(r"'(\w+)'", rarity)) == {
            "common", "uncommon", "rare", "mythic", "special", "bonus", "unknown"}
        types = re.search(r"const MAIN_TYPES = \[([^\]]+)\]", js).group(1)
        assert set(re.findall(r"'(\w+)'", types)) | {"Other"} == {
            "Creature", "Instant", "Sorcery", "Enchantment", "Artifact",
            "Planeswalker", "Battle", "Land", "Other"}


class TestAmbientBackground:
    async def test_layer_and_script_come_first(self, client):
        r = await client.get("/", headers=HTML)
        body = r.text.split("<body", 1)[1]
        layer = body.index('class="fx-ambient-layer"')
        script = body.index("/static/js/ambient.js")
        shell = body.index('class="app-shell')
        assert layer < script < shell

    async def test_ambient_script_is_not_deferred(self, client):
        """Tiene que pintar antes del primer fotograma."""
        r = await client.get("/", headers=HTML)
        tag = re.search(r"<script[^>]*/static/js/ambient\.js[^>]*>", r.text).group(0)
        assert "defer" not in tag and "async" not in tag

    async def test_toggle_is_translated(self, client):
        r = await client.get("/", headers=HTML)
        assert 'data-label-on="Desactivar el fondo animado"' in r.text
        client.cookies.set("lang", "en")
        r = await client.get("/", headers=HTML)
        assert 'data-label-off="Turn on the animated background"' in r.text

    async def test_preference_is_applied_before_first_paint(self, client):
        r = await client.get("/", headers=HTML)
        head = r.text.split("</head>", 1)[0]
        assert "mpc-ambient" in head and "fx-ambient-off" in head

    async def test_present_on_every_page(self, client):
        for path in ("/", "/decks", "/history", "/collection", "/settings",
                     "/print-planner", "/art-library", "/calibrate", "/no-existe"):
            r = await client.get(path, headers=HTML)
            assert 'class="fx-ambient-layer"' in r.text, path

    def test_reduced_motion_is_respected(self):
        js = _read("static/js/ambient.js")
        assert "prefers-reduced-motion" in js
        css = _read("static/motion.css")
        reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        assert "prefers-reduced-motion" in css
