from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"

PAGINATED_ENDPOINTS = [
    ("/prints", "api.cards.prints() o api.cards.allPrints()"),
    ("/api/library/browse", "api.request sobre /api/library/browse"),
]

EXEMPT = {"api.js"}


def js_files() -> list[Path]:
    return sorted(p for p in JS_DIR.glob("*.js") if p.name not in EXEMPT)


@pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
@pytest.mark.parametrize(
    "endpoint,helper", PAGINATED_ENDPOINTS, ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_raw_fetch_against_paginated_endpoint(path, endpoint, helper):
    source = path.read_text(encoding="utf-8")

    without_comments = re.sub(r"//[^\n]*", "", source)
    without_comments = re.sub(r"/\*.*?\*/", "", without_comments, flags=re.DOTALL)

    offenders = [
        match.group(0)[:90]
        for match in re.finditer(r"fetch\([^)]*\)", without_comments, re.DOTALL)
        if endpoint in match.group(0)
    ]

    assert not offenders, (
        f"{path.name} llama con fetch() directo a un endpoint paginado "
        f"({endpoint}):\n  "
        + "\n  ".join(offenders)
        + f"\n\nEsa respuesta es un sobre {{items, custom, total, has_more}}, no "
        f"un array: hacer .filter() sobre ella lanza "
        f"'X.filter is not a function'. Usa {helper}."
    )


class TestAllPrintsHelper:
    @pytest.fixture(scope="class")
    def api_js(self) -> str:
        return (JS_DIR / "api.js").read_text(encoding="utf-8")

    def test_it_exists(self, api_js):
        assert "allPrints:" in api_js

    def test_it_merges_custom_arts_first(self, api_js):
        body = api_js[api_js.index("allPrints:") :]
        body = body[: body.index("\n    },")]
        assert "first.custom" in body
        assert body.index("first.custom") < body.index("first.items")

    def test_it_follows_has_more(self, api_js):
        body = api_js[api_js.index("allPrints:") :]
        body = body[: body.index("\n    },")]
        assert "has_more" in body, "Sin seguir has_more solo traería una página"

    def test_it_has_a_page_ceiling(self, api_js):
        body = api_js[api_js.index("allPrints:") :]
        body = body[: body.index("\n    },")]
        assert "maxPages" in body


class TestKnownConsumersUseTheHelper:
    @pytest.mark.parametrize(
        "module,function_name",
        [
            ("pdf-studio.js", "openMiniArtPicker"),
            ("deck-editor.js", "selectCard"),
        ],
    )
    def test_consumer_uses_all_prints(self, module, function_name):
        source = (JS_DIR / module).read_text(encoding="utf-8")
        assert function_name in source, f"{function_name} ya no existe en {module}"
        assert "allPrints" in source, (
            f"{module} debe obtener las opciones de arte con "
            f"api.cards.allPrints(), no paginando a mano ni con fetch directo."
        )
