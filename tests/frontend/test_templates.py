from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from tests.frontend._support import (
    HTML,
    INLINE_SCRIPT,
    JS,
    PAGE_MODULES,
    SCRIPT_TAG,
    TEMPLATES,
    WINDOW_EXPORT,
    position,
    read,
    render,
    scripts_in_execution_order,
    template_source,
)

MAX_INLINE_JS_LINES = 5

EAGER_ATTRS = (
    "x-text",
    "x-html",
    "x-model",
    ":class",
    ":style",
    ":href",
    ":src",
    ":value",
    ":disabled",
    "x-show",
)
NULL_PROP = re.compile(r"^\s{4}([A-Za-z_$][\w$]*)\s*:\s*null\s*,", re.MULTILINE)

HOSTILE_NAMES = [
    "[Video Guide] Y'shtola PRECON UPGRADES (25 Cards)",
    'Deck "con comillas"',
    "Barra \\ invertida",
    "</script><b>x</b> & más",
]


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return {name: render(name) for name in PAGE_MODULES}


class TestPageScripts:
    def test_pages_render_completely(self, rendered):
        problems = []
        for page, html in rendered.items():
            if len(html) < 500:
                problems.append(f"{page}: renderizó casi vacío")
            if "{%" in html or "{{" in html:
                problems.append(f"{page}: deja Jinja sin resolver")
            for match in SCRIPT_TAG.finditer(html):
                if match.group("src").startswith("http"):
                    problems.append(f"{page}: script remoto {match.group('src')}")
        assert not problems, "\n".join(problems)

    def test_view_module_runs_after_api_and_before_alpine(self, rendered):
        problems = []
        for page, module in PAGE_MODULES.items():
            order = scripts_in_execution_order(rendered[page])
            api, view, alpine = (
                position(order, "core/api.js"),
                position(order, module),
                position(order, "alpine.min.js"),
            )
            plugins = [
                position(order, p) for p in ("alpine-collapse.min.js", "alpine-focus.min.js")
            ]
            if min(api, view, alpine, *plugins) < 0:
                problems.append(f"{page}: falta un script esencial en {order}")
            elif not (api < view < alpine and max(plugins) < alpine):
                problems.append(f"{page}: orden incorrecto {order}")
        assert not problems, (
            "Alpine arranca nada más ejecutarse: el cliente API, los plugins y el "
            "módulo de la vista deben ejecutarse antes.\n" + "\n".join(problems)
        )

    def test_each_page_declares_its_module_with_cache_busting(self):
        problems = []
        for page, module in PAGE_MODULES.items():
            html = read(TEMPLATES / page)
            tag = re.search(rf'<script[^>]*src="/static/js/{re.escape(module)}[^>]*>', html)
            block = html.split("{% block view_module %}", 1)[-1].split("{% endblock %}", 1)[0]
            if tag is None or tag.group(0) not in block:
                problems.append(f"{page}: no carga {module} en el bloque view_module")
            elif "defer" not in tag.group(0) or f"asset_v('js/{module}')" not in tag.group(0):
                problems.append(f"{page}: {module} sin defer o sin asset_v")
            inline = "\n".join(INLINE_SCRIPT.findall(template_source(page)))
            if len([ln for ln in inline.splitlines() if ln.strip()]) > MAX_INLINE_JS_LINES:
                problems.append(f"{page}: acumula JS embebido; muévelo a static/js/")
        assert not problems, "\n".join(problems)

    def test_x_data_factories_are_exposed_on_window(self, rendered):
        shared = set()
        for core in ("core/api.js", "core/shell.js"):
            shared |= set(WINDOW_EXPORT.findall(read(JS / core)))
        problems = []
        for page, module in PAGE_MODULES.items():
            exposed = shared | set(WINDOW_EXPORT.findall(read(JS / module)))
            if page == "pages/landing.html":
                exposed |= set(WINDOW_EXPORT.findall(read(JS / "pages/home.js")))
            referenced = set()
            for expr in re.findall(r'x-data="([^"]*)"', rendered[page]):
                referenced.update(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", expr))
            if missing := referenced - exposed:
                problems.append(f"{page}: {sorted(missing)}")
        assert not problems, "x-data usa funciones que nadie publica en window:\n" + "\n".join(
            problems
        )

    def test_base_layout_contract(self):
        base = read(TEMPLATES / "base.html")
        assert base.index("js/core/api.js") < base.index("{% block view_module %}")
        assert base.index("{% block view_module %}") < base.index("vendor/alpine.min.js")
        assert base.index("js/core/shell.js") < base.index("vendor/alpine.min.js")
        for partial in ("sidebar", "toasts", "confirm_dialog"):
            assert f'{{% include "partials/shell/{partial}.html" %}}' in base


def _unsafe_dereferences(html: str, prop: str) -> list[str]:
    html = re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)
    guarded = []
    for match in re.finditer(
        r'<template\s+x-if="[^"]*\b' + re.escape(prop) + r'\b[^"]*"', html, re.DOTALL
    ):
        depth, end = 0, match.start()
        for tag in re.finditer(r"</?template\b", html[match.start() :]):
            depth += -1 if tag.group().startswith("</") else 1
            if depth == 0:
                end = match.start() + tag.end()
                break
        guarded.append((match.start(), end))

    attr = re.compile("(" + "|".join(re.escape(a) for a in EAGER_ATTRS) + r')="([^"]*)"')
    offenders = []
    for match in attr.finditer(html):
        expr = match.group(2)
        if not re.search(rf"\b{re.escape(prop)}\.", expr):
            continue
        if re.search(rf"\b{re.escape(prop)}\?\.", expr):
            continue
        if re.search(rf"\b{re.escape(prop)}\b\s*(&&|\?[^.])", expr):
            continue
        if any(start <= match.start() <= end for start, end in guarded):
            continue
        offenders.append(f'{match.group(1)}="{expr[:80]}"')
    return offenders


class TestNullSafety:
    def test_null_guard_detector(self, tmp_path):
        flagged = [
            '<div x-text="fmt(result.x)"></div>',
            '<section x-show="result"><div x-text="fmt(result.x)"></div></section>',
        ]
        accepted = [
            '<div x-text="fmt(result?.x)"></div>',
            '<template x-if="result"><div x-text="fmt(result.x)"></div></template>',
            '<div x-show="result"></div>',
            '<div x-text="result && result.x"></div>',
            """<div x-text="result ? result.name : ''"></div>""",
            '{# mal: x-text="fmt(result.x)" #}<div x-text="ok"></div>',
        ]
        assert all(_unsafe_dereferences(html, "result") for html in flagged)
        assert not any(_unsafe_dereferences(html, "result") for html in accepted)
        module = "  return {\n    result: null,\n    plan: null,\n    items: [],\n  }\n"
        assert set(NULL_PROP.findall(module)) == {"result", "plan"}

    def test_views_never_dereference_state_that_starts_as_null(self):
        problems = []
        for page, module in PAGE_MODULES.items():
            html = template_source(page)
            for prop in sorted(NULL_PROP.findall(read(JS / module))):
                if prop.startswith("_"):
                    continue
                problems += [f"{page} · {prop}: {o}" for o in _unsafe_dereferences(html, prop)]
        assert not problems, (
            "x-show NO protege: usa <template x-if> o encadenamiento opcional.\n"
            + "\n".join(problems)
        )


class _AlpineAttrs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.exprs: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (
                name in ("x-text", "x-data", "x-show", "x-html")
                or name.startswith(("@", ":", "x-on:"))
            ):
                self.exprs.append(value)


def _alpine_exprs(html: str) -> list[str]:
    parser = _AlpineAttrs()
    parser.feed(html)
    return parser.exprs


@pytest.mark.skipif(shutil.which("node") is None, reason="necesita node")
async def test_deck_names_are_safe_inside_alpine_expressions(client):
    exprs = []
    for name in HOSTILE_NAMES:
        r = await client.post(
            "/api/decks/import/text",
            json={"name": name, "text": "1 Sol Ring", "format": "commander"},
        )
        deck = r.json()["deck"]
        pdf = await client.get(f"/decks/{deck['id']}/pdf", headers=HTML)
        deck_exprs = [e for e in _alpine_exprs(pdf.text) if "deck.name" in e]
        assert len(deck_exprs) == 2
        assert all(json.loads(e.split(" : ", 1)[1]) == name for e in deck_exprs)
        exprs += deck_exprs

    library = await client.get("/decks", headers=HTML)
    menu = [
        e for e in _alpine_exprs(library.text) if e.startswith(("rename(", "duplicate(", "del("))
    ]
    assert len(menu) == 3 * len(HOSTILE_NAMES)
    exprs += menu

    script = (
        "const exprs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "for (const e of exprs) {"
        "  try { new Function('with (this) { return (' + e + ') }'); }"
        "  catch (_) { new Function('with (this) { ' + e + ' }'); }"
        "}"
    )
    proc = subprocess.run(
        ["node", "-e", script], input=json.dumps(exprs), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


class TestDeckEditorStats:
    def test_stats_modal_is_pure_svg_and_css(self):
        js = read(JS / "pages/deck-editor.js")
        modal = read(TEMPLATES / "partials/deck/stats_modal.html")
        page = read(TEMPLATES / "pages/deck.html")
        assert "new Chart(" not in js and "_chartInstances" not in js and "<canvas" not in modal
        assert "/static/css/stats.css?v={{ asset_v('css/stats.css') }}" in page
        assert all("x-for" not in svg for svg in re.findall(r"<svg.*?</svg>", modal, flags=re.S))

    def test_stats_keys_are_translated(self):
        from mpc_forge.services.system.i18n import _TRANSLATIONS

        js = read(JS / "pages/deck-editor.js")
        colors = re.findall(r"'(\w)'", re.search(r"const CURVE_COLORS = \[([^\]]+)\]", js).group(1))
        rarity = re.findall(
            r"'(\w+)'", re.search(r"const RARITY_ORDER = \[([^\]]+)\]", js).group(1)
        )
        types = re.findall(r"'(\w+)'", re.search(r"const MAIN_TYPES = \[([^\]]+)\]", js).group(1))
        assert colors == ["W", "U", "B", "R", "G", "M", "C"]
        expected = {
            "stats_color_": colors,
            "stats_rarity_": rarity,
            "stats_type_": [*types, "Other"],
        }
        for lang, table in _TRANSLATIONS.items():
            for family, keys in expected.items():
                assert not [k for k in keys if family + k not in table], (lang, family)

    async def test_stats_modal_renders_translated(self, client, deck):
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert r.status_code == 200 and "Estadísticas del mazo" in r.text
        client.cookies.set("lang", "en")
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert "Deck statistics" in r.text and "Curva de maná" not in r.text


class TestAmbientBackground:
    async def test_layer_is_painted_before_the_shell_on_every_page(self, client):
        for path in ("/", "/decks", "/history", "/collection", "/settings", "/no-existe"):
            r = await client.get(path, headers=HTML)
            body = r.text.split("<body", 1)[1]
            layer = body.index('class="fx-ambient-layer"')
            tag = re.search(r"<script[^>]*/static/js/core/ambient\.js[^>]*>", body)
            assert layer < tag.start() < body.index('class="app-shell'), path
            assert "defer" not in tag.group(0) and "async" not in tag.group(0)

        head = r.text.split("</head>", 1)[0]
        assert "mpc-ambient" in head and "fx-ambient-off" in head

    async def test_toggle_is_translated_and_motion_preferences_respected(self, client):
        r = await client.get("/", headers=HTML)
        assert 'data-label-on="Desactivar el fondo animado"' in r.text
        client.cookies.set("lang", "en")
        r = await client.get("/", headers=HTML)
        assert 'data-label-off="Turn on the animated background"' in r.text
        assert "prefers-reduced-motion" in read(JS / "core/ambient.js")
        assert "@media (prefers-reduced-motion: reduce)" in read(
            TEMPLATES.parent / "static/css/motion.css"
        )
