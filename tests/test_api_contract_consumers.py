"""Consumidores desactualizados de endpoints con contrato paginado.

El fallo que motiva este fichero
--------------------------------
En el Sprint 2, ``/api/decks/{id}/cards/{id}/prints`` pasó de devolver un array
plano a devolver un sobre paginado ``{items, custom, total, has_more}``.
Actualicé el selector de arte grande y el test que dependía del formato… y me
dejé DOS consumidores más, cada uno en un fichero distinto:

* el mini selector del PDF Studio → ``arts.filter is not a function``
* el panel lateral del editor de mazos → mismo problema

Ninguno de los dos falló en la suite de Python, porque son JavaScript, ni en el
humo de navegador, porque solo se ejecutan al hacer clic en una carta.

Qué comprueba este test
-----------------------
Que ningún fichero de ``static/js`` llame con ``fetch()`` directo a un endpoint
cuyo contrato sea un sobre paginado. Esos endpoints se consumen a través de
``api.js``, que es el único sitio donde se sabe cómo se paginan.

No es una regla estética: centralizar el acceso es lo que hace que cambiar un
contrato sea un cambio en un fichero en vez de una búsqueda a ciegas por todo
el frontend.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"

# Endpoints cuya respuesta NO es un array plano y que por tanto no se pueden
# consumir con un `fetch` suelto seguido de `.filter()` / `.map()`.
# (fragmento de ruta, helper de api.js que hay que usar en su lugar)
PAGINATED_ENDPOINTS = [
    ("/prints", "api.cards.prints() o api.cards.allPrints()"),
    ("/api/library/browse", "api.request sobre /api/library/browse"),
]

# `api.js` es la excepción: es justamente el sitio donde vive el conocimiento
# de cómo se pagina cada endpoint.
EXEMPT = {"api.js"}


def js_files() -> list[Path]:
    return sorted(p for p in JS_DIR.glob("*.js") if p.name not in EXEMPT)


@pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
@pytest.mark.parametrize("endpoint,helper", PAGINATED_ENDPOINTS,
                         ids=lambda v: v if isinstance(v, str) else "")
def test_no_raw_fetch_against_paginated_endpoint(path, endpoint, helper):
    source = path.read_text(encoding="utf-8")

    # Se buscan llamadas a fetch(...) que mencionen el endpoint. Los
    # comentarios se descartan: varios explican precisamente esta historia.
    without_comments = re.sub(r"//[^\n]*", "", source)
    without_comments = re.sub(r"/\*.*?\*/", "", without_comments, flags=re.DOTALL)

    offenders = [
        match.group(0)[:90]
        for match in re.finditer(r"fetch\([^)]*\)", without_comments, re.DOTALL)
        if endpoint in match.group(0)
    ]

    assert not offenders, (
        f"{path.name} llama con fetch() directo a un endpoint paginado "
        f"({endpoint}):\n  " + "\n  ".join(offenders) +
        f"\n\nEsa respuesta es un sobre {{items, custom, total, has_more}}, no "
        f"un array: hacer .filter() sobre ella lanza "
        f"'X.filter is not a function'. Usa {helper}."
    )


class TestAllPrintsHelper:
    """El helper que evita que cada consumidor pagine por su cuenta."""

    @pytest.fixture(scope="class")
    def api_js(self) -> str:
        return (JS_DIR / "api.js").read_text(encoding="utf-8")

    def test_it_exists(self, api_js):
        assert "allPrints:" in api_js

    def test_it_merges_custom_arts_first(self, api_js):
        """El contrato antiguo ponía los customs delante, y los consumidores
        que solo quieren "dame todo" dependen de ese orden."""
        body = api_js[api_js.index("allPrints:"):]
        body = body[:body.index("\n    },")]
        assert "first.custom" in body
        assert body.index("first.custom") < body.index("first.items")

    def test_it_follows_has_more(self, api_js):
        body = api_js[api_js.index("allPrints:"):]
        body = body[:body.index("\n    },")]
        assert "has_more" in body, "Sin seguir has_more solo traería una página"

    def test_it_has_a_page_ceiling(self, api_js):
        """Un `has_more` que nunca baje no debe colgar el navegador."""
        body = api_js[api_js.index("allPrints:"):]
        body = body[:body.index("\n    },")]
        assert "maxPages" in body


class TestKnownConsumersUseTheHelper:
    """Los dos sitios que se rompieron, comprobados por nombre.

    Es redundante con el test genérico de arriba, pero un fallo aquí señala
    directamente al fichero afectado en lugar de a una regla abstracta.
    """

    @pytest.mark.parametrize("module,function_name", [
        ("pdf-studio.js", "openMiniArtPicker"),
        ("deck-editor.js", "selectCard"),
    ])
    def test_consumer_uses_all_prints(self, module, function_name):
        source = (JS_DIR / module).read_text(encoding="utf-8")
        assert function_name in source, f"{function_name} ya no existe en {module}"
        assert "allPrints" in source, (
            f"{module} debe obtener las opciones de arte con "
            f"api.cards.allPrints(), no paginando a mano ni con fetch directo."
        )
