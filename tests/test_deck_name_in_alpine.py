from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

HOSTILE_NAMES = [
    "[Video Guide] Y'shtola PRECON UPGRADES (25 Cards)",
    'Deck "con comillas"',
    "Barra \\ invertida",
    "</script><b>x</b> & más",
]

ALPINE_ATTRS = ("x-text", "x-data", "x-show", "x-html")


class _AlpineAttrs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.exprs: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if value and (name in ALPINE_ATTRS or name.startswith(("@", ":", "x-on:"))):
                self.exprs.append(value)


def _alpine_exprs(html: str) -> list[str]:
    parser = _AlpineAttrs()
    parser.feed(html)
    return parser.exprs


def _assert_compiles(exprs: list[str]) -> None:
    script = (
        "const exprs = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        "for (const e of exprs) {"
        "  try { new Function('with (this) { return (' + e + ') }'); }"
        "  catch (_) { new Function('with (this) { ' + e + ' }'); }"
        "}"
    )
    proc = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(exprs),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


async def _make_deck(client, name: str) -> dict:
    r = await client.post(
        "/api/decks/import/text",
        json={
            "name": name,
            "text": "1 Sol Ring",
            "format": "commander",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["deck"]


@pytest.mark.skipif(shutil.which("node") is None, reason="necesita node")
@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_pdf_studio_expressions_compile(client, name):
    deck = await _make_deck(client, name)
    r = await client.get(f"/decks/{deck['id']}/pdf", headers=HTML)
    assert r.status_code == 200
    exprs = [e for e in _alpine_exprs(r.text) if "deck.name" in e]
    assert len(exprs) == 2
    for e in exprs:
        fallback = e.split(" : ", 1)[1]
        assert json.loads(fallback) == name
    _assert_compiles(exprs)


@pytest.mark.skipif(shutil.which("node") is None, reason="necesita node")
@pytest.mark.parametrize("name", HOSTILE_NAMES)
async def test_library_menu_expressions_compile(client, name):
    await _make_deck(client, name)
    r = await client.get("/decks", headers=HTML)
    assert r.status_code == 200
    exprs = [e for e in _alpine_exprs(r.text) if e.startswith(("rename(", "duplicate(", "del("))]
    assert len(exprs) == 3
    _assert_compiles(exprs)
