from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from mpc_forge.services.system.i18n import _TRANSLATIONS
from tests.frontend._support import HTML, JS, TEMPLATES, read

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="necesita node")

PAGE_URLS = {
    "home": "/",
    "decks": "/decks",
    "deck": "/decks/{id}",
    "pdf": "/decks/{id}/pdf",
    "proof": "/decks/{id}/proof",
    "history": "/history",
    "collection": "/collection",
    "planner": "/print-planner",
    "library": "/art-library",
    "calibrate": "/calibrate",
    "settings": "/settings",
    "welcome": "/",
}
TARGET = re.compile(r'^\[data-tour="([a-z0-9-]+)"\]$')


@pytest.fixture(scope="module")
def tours() -> dict:
    script = (
        "const fs = require('fs'); const window = {};"
        f"eval(fs.readFileSync({json.dumps(str(JS / 'core/tour-steps.js'))}, 'utf8'));"
        "const paths = " + json.dumps({k: v.format(id=7) for k, v in PAGE_URLS.items()}) + ";"
        "const routed = {}; for (const [k, p] of Object.entries(paths)) routed[k] = window.tourForPath(p);"
        "console.log(JSON.stringify({tours: window.TOURS, routed}));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_every_page_maps_to_its_own_tour(tours):
    routed = tours["routed"]
    assert {k: v for k, v in routed.items() if k != "welcome"} == {
        k: k for k in PAGE_URLS if k != "welcome"
    }
    assert set(tours["tours"]) == set(PAGE_URLS)


@needs_node
def test_every_step_is_translated(tours):
    missing = []
    for lang, table in _TRANSLATIONS.items():
        for tour_id, tour in tours["tours"].items():
            keys = [f"tour_label_{tour_id}"]
            for step in tour["steps"]:
                keys += [f"tour_{tour_id}_{step['id']}_title", f"tour_{tour_id}_{step['id']}_body"]
            missing += [f"{lang}:{k}" for k in keys if not str(table.get(k, "")).strip()]
    assert not missing, missing


@needs_node
async def test_every_target_exists_on_its_page(client, deck, tours):
    problems = []
    for tour_id, tour in tours["tours"].items():
        html = (await client.get(PAGE_URLS[tour_id].format(id=deck["id"]), headers=HTML)).text
        for step in tour["steps"]:
            if not step.get("target") or step.get("dynamic"):
                continue
            selectors = step["target"] if isinstance(step["target"], list) else [step["target"]]
            names = []
            for sel in selectors:
                m = TARGET.match(sel)
                assert m, f'{tour_id}.{step["id"]}: usa [data-tour="…"] ({sel})'
                names.append(m.group(1))
            if not any(f'data-tour="{n}"' in html for n in names):
                problems.append(f"{tour_id}.{step['id']} → {names}")
    assert not problems, problems


@needs_node
def test_dynamic_targets_come_from_the_settings_navigation(tours):
    nav = read(TEMPLATES / "partials/settings/navigation.html")
    settings_js = read(JS / "pages/settings.js")
    assert ":data-tour=\"'settings-nav-' + s.key\"" in nav
    for step in tours["tours"]["settings"]["steps"]:
        if step.get("dynamic"):
            key = step["target"].split("settings-nav-", 1)[1].split('"', 1)[0]
            assert f"key: '{key}'" in settings_js, key


def test_tour_is_part_of_every_page_and_can_be_replayed():
    base = read(TEMPLATES / "base.html")
    assert base.index("js/core/tour-steps.js") < base.index("js/core/tour.js")
    assert base.index("js/core/tour.js") < base.index("vendor/alpine.min.js")
    assert '{% include "partials/shell/tour.html" %}' in base
    assert "css/tour.css" in base
    sidebar = read(TEMPLATES / "partials/shell/sidebar.html")
    assert 'data-tour="nav-tour"' in sidebar and "$store.tour.replayPage()" in sidebar
    assert "$store.tour.restartAll()" in sidebar
    general = read(TEMPLATES / "partials/settings/section_general.html")
    assert "$store.tour.restartAll()" in general and "$store.tour.setAuto(" in general


class TestTourState:
    async def test_first_visit_then_seen_reset_and_auto(self, client):
        state = (await client.get("/api/tour/")).json()
        assert state == {"seen": [], "auto": True}

        r = await client.post("/api/tour/seen", json={"tour": "welcome"})
        assert r.json() == {"seen": ["welcome"], "auto": True}
        await client.post("/api/tour/seen", json={"tour": "deck"})
        await client.post("/api/tour/seen", json={"tour": "welcome"})
        assert (await client.get("/api/tour/")).json()["seen"] == ["welcome", "deck"]

        assert (await client.post("/api/tour/seen", json={"tour": "../x"})).status_code == 400
        assert (await client.post("/api/tour/seen", json={"tour": ""})).status_code == 422

        r = await client.put("/api/tour/auto", json={"auto": False})
        assert r.json() == {"seen": ["welcome", "deck"], "auto": False}

        r = await client.delete("/api/tour/")
        assert r.json() == {"seen": [], "auto": True}

    async def test_corrupt_state_falls_back_to_first_visit(self, client):
        from mpc_forge.db import session_scope
        from mpc_forge.models import KeyValue

        async with session_scope() as db:
            db.add(KeyValue(key="ui.tour", value="{no es json"))
            await db.commit()
        assert (await client.get("/api/tour/")).json() == {"seen": [], "auto": True}
