"""Blindaje de la superficie de la API y de la estructura del paquete de rutas.

`routes/decks.py` tenía 2.226 líneas con nueve responsabilidades distintas. Al
dividirlo en sub-routers, el riesgo era perder o duplicar una ruta sin que
nadie se diera cuenta: un endpoint que desaparece no rompe ningún test de
Python, solo deja de funcionar en la interfaz.

Estos tests comparan el esquema OpenAPI actual con la instantánea tomada justo
antes de la división, y comprueban además las trampas de ordenación de rutas
que FastAPI no avisa.
"""
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).parent / "data_openapi_snapshot.json"
DECKS_PKG = ROOT / "mpc_forge" / "routes" / "decks"

# Máximo de líneas por módulo de rutas. El objetivo de la división era que
# cada fichero cupiera en la cabeza de quien lo lee; sin un tope explícito
# vuelven a crecer sin que nadie lo note.
MAX_MODULE_LINES = 1000


@pytest.fixture(scope="module")
def current_schema():
    warnings.filterwarnings("ignore")
    from mpc_forge.app import create_app
    return create_app().openapi()


@pytest.fixture(scope="module")
def current_operations(current_schema):
    ops = {}
    for path, methods in current_schema["paths"].items():
        for method, op in methods.items():
            key = f"{method.upper()} {path}"
            ops[key] = sorted(
                (p["name"], p["in"], p.get("required", False))
                for p in op.get("parameters", [])
            )
    return ops


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


class TestApiSurfaceIsUnchanged:
    def test_snapshot_exists(self):
        assert SNAPSHOT.exists(), (
            "Falta la instantánea de OpenAPI. Se generó antes de dividir "
            "routes/decks.py y sirve de referencia de que ninguna ruta se "
            "perdió."
        )

    def test_no_endpoint_disappeared(self, current_operations, snapshot):
        missing = sorted(set(snapshot) - set(current_operations))
        assert not missing, (
            "Estos endpoints existían y ya no:\n  " + "\n  ".join(missing) +
            "\n\nSi el borrado es intencionado, regenera la instantánea."
        )

    def test_no_parameters_changed(self, current_operations, snapshot):
        """Cambiar un parámetro de obligatorio a opcional (o al revés) rompe
        al cliente sin que falle ningún test de servidor."""
        changed = []
        for key in sorted(set(snapshot) & set(current_operations)):
            before = [tuple(p) for p in snapshot[key]]
            now = current_operations[key]
            if before != now:
                changed.append(f"{key}\n    antes: {before}\n    ahora: {now}")
        assert not changed, "Parámetros alterados:\n" + "\n".join(changed)

    def test_deck_endpoints_are_all_present(self, current_operations, snapshot):
        """Comprobación específica del paquete que se dividió.

        Se vigilan las DESAPARICIONES, no el crecimiento: añadir endpoints es
        el curso normal del desarrollo, mientras que perder uno al reorganizar
        ficheros es el accidente que esta instantánea existe para detectar.
        """
        before = {k for k in snapshot if "/api/decks" in k}
        now = {k for k in current_operations if "/api/decks" in k}
        missing = sorted(before - now)
        assert not missing, (
            "Estas rutas de mazos existían y ya no:\n  " + "\n  ".join(missing)
        )
        assert len(now) >= len(before), (
            f"Hay {len(now)} rutas de mazos y antes había {len(before)}"
        )


class TestDecksPackageStructure:
    def test_old_monolith_is_gone(self):
        assert not (ROOT / "mpc_forge" / "routes" / "decks.py").exists(), (
            "routes/decks.py ha vuelto. La lógica debe vivir en el paquete "
            "routes/decks/."
        )

    def test_package_exists_with_expected_modules(self):
        expected = {
            "__init__.py", "_common.py", "_views.py", "imports.py", "crud.py",
            "search.py", "activity.py", "tokens.py", "localize.py", "art.py",
        }
        actual = {p.name for p in DECKS_PKG.glob("*.py")}
        assert expected <= actual, f"Faltan módulos: {sorted(expected - actual)}"

    @pytest.mark.parametrize("module", sorted(
        p.name for p in DECKS_PKG.glob("*.py")
    ) if DECKS_PKG.exists() else [])
    def test_module_stays_readable(self, module):
        lines = len((DECKS_PKG / module).read_text(encoding="utf-8").splitlines())
        assert lines <= MAX_MODULE_LINES, (
            f"routes/decks/{module} tiene {lines} líneas (tope "
            f"{MAX_MODULE_LINES}). Divídelo antes de que vuelva a ser un "
            f"monolito."
        )

    @pytest.mark.parametrize("module", sorted(
        p.name for p in DECKS_PKG.glob("*.py")
    ) if DECKS_PKG.exists() else [])
    def test_module_has_a_docstring(self, module):
        import ast
        source = (DECKS_PKG / module).read_text(encoding="utf-8")
        assert ast.get_docstring(ast.parse(source)), (
            f"routes/decks/{module} no explica de qué se ocupa"
        )

    def test_shared_helpers_live_in_views_module(self):
        """Si los serializadores vuelven a un sub-router, aparecen ciclos."""
        views = (DECKS_PKG / "_views.py").read_text(encoding="utf-8")
        for helper in ("_deck_to_view", "_deckcard_to_view", "_deckcards_to_views"):
            assert f"def {helper}(" in views, f"{helper} no está en _views.py"


class TestRouteOrdering:
    """FastAPI resuelve por orden de registro, sin avisar de las colisiones.

    Las rutas literales bajo `/_/` tienen que registrarse ANTES que
    `/{deck_id}`, o una petición a `/api/decks/_/autocomplete` entraría por la
    paramétrica con `deck_id="_"` y devolvería un 422 en vez del resultado.
    """

    # (ruta en el esquema, ruta a pedir con sus parámetros obligatorios).
    # `search-cards` y `autocomplete` exigen `q`; sin él devolverían un 422
    # legítimo que se confundiría con la colisión que queremos detectar.
    LITERAL_ROUTES = [
        ("/api/decks/_/search-cards", "/api/decks/_/search-cards?q=sol"),
        ("/api/decks/_/with-activity", "/api/decks/_/with-activity"),
        ("/api/decks/_/undoable-kinds", "/api/decks/_/undoable-kinds"),
        ("/api/decks/_/supported-langs", "/api/decks/_/supported-langs"),
        ("/api/decks/_/autocomplete", "/api/decks/_/autocomplete?q=sol"),
    ]

    @pytest.mark.parametrize("route,_url", LITERAL_ROUTES, ids=lambda v: v)
    def test_literal_route_is_registered(self, route, _url, current_schema):
        assert route in current_schema["paths"], f"{route} no está registrada"

    @pytest.mark.parametrize("route,url", LITERAL_ROUTES, ids=lambda v: v)
    async def test_literal_route_does_not_collide(self, client, route, url):
        """No basta con que la ruta exista: hay que probar que responde.

        Si la paramétrica la capturara, el esquema seguiría listándola pero la
        petición real devolvería 422.
        """
        response = await client.get(url)
        assert response.status_code != 422, (
            f"{route} la está capturando /{{deck_id}}: FastAPI intentó "
            f"interpretar '_' como un id de mazo. Registra el sub-router de "
            f"rutas literales antes que 'crud' en routes/decks/__init__.py."
        )
        assert response.status_code < 500

    def test_init_registers_literal_routers_first(self):
        """El orden correcto se documenta y se comprueba, no se confía."""
        init = (DECKS_PKG / "__init__.py").read_text(encoding="utf-8")
        order = []
        for name in ("search", "activity", "localize", "crud"):
            marker = f"router.include_router({name}.router)"
            assert marker in init, f"{name} no se incluye en __init__.py"
            order.append((init.index(marker), name))
        order.sort()
        names = [n for _, n in order]
        assert names[-1] == "crud", (
            f"'crud' (dueño de /{{deck_id}}) debe registrarse el último, pero "
            f"el orden es {names}"
        )
