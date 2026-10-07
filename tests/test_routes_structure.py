from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(__file__).parent / "data_openapi_snapshot.json"
DECKS_PKG = ROOT / "mpc_forge" / "routes" / "decks"

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
                (p["name"], p["in"], p.get("required", False)) for p in op.get("parameters", [])
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
            "Estos endpoints existían y ya no:\n  "
            + "\n  ".join(missing)
            + "\n\nSi el borrado es intencionado, regenera la instantánea."
        )

    def test_no_parameters_changed(self, current_operations, snapshot):
        changed = []
        for key in sorted(set(snapshot) & set(current_operations)):
            before = [tuple(p) for p in snapshot[key]]
            now = current_operations[key]
            if before != now:
                changed.append(f"{key}\n    antes: {before}\n    ahora: {now}")
        assert not changed, "Parámetros alterados:\n" + "\n".join(changed)

    def test_deck_endpoints_are_all_present(self, current_operations, snapshot):
        before = {k for k in snapshot if "/api/decks" in k}
        now = {k for k in current_operations if "/api/decks" in k}
        missing = sorted(before - now)
        assert not missing, "Estas rutas de mazos existían y ya no:\n  " + "\n  ".join(missing)
        assert len(now) >= len(before), f"Hay {len(now)} rutas de mazos y antes había {len(before)}"


class TestDecksPackageStructure:
    def test_old_monolith_is_gone(self):
        assert not (ROOT / "mpc_forge" / "routes" / "decks.py").exists(), (
            "routes/decks.py ha vuelto. La lógica debe vivir en el paquete routes/decks/."
        )

    def test_package_exists_with_expected_modules(self):
        expected = {
            "__init__.py",
            "_common.py",
            "_views.py",
            "imports.py",
            "crud.py",
            "search.py",
            "activity.py",
            "tokens.py",
            "localize.py",
            "art.py",
        }
        actual = {p.name for p in DECKS_PKG.glob("*.py")}
        assert expected <= actual, f"Faltan módulos: {sorted(expected - actual)}"

    def test_shared_helpers_live_in_views_module(self):
        views = (DECKS_PKG / "_views.py").read_text(encoding="utf-8")
        for helper in ("_deck_to_view", "_deckcard_to_view", "_deckcards_to_views"):
            assert f"def {helper}(" in views, f"{helper} no está en _views.py"


class TestRouteOrdering:
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
        response = await client.get(url)
        assert response.status_code != 422, (
            f"{route} la está capturando /{{deck_id}}: FastAPI intentó "
            f"interpretar '_' como un id de mazo. Registra el sub-router de "
            f"rutas literales antes que 'crud' en routes/decks/__init__.py."
        )
        assert response.status_code < 500

    def test_init_registers_literal_routers_first(self):
        init = (DECKS_PKG / "__init__.py").read_text(encoding="utf-8")
        order = []
        for name in ("search", "activity", "localize", "crud"):
            marker = f"router.include_router({name}.router)"
            assert marker in init, f"{name} no se incluye en __init__.py"
            order.append((init.index(marker), name))
        order.sort()
        names = [n for _, n in order]
        assert names[-1] == "crud", (
            f"'crud' (dueño de /{{deck_id}}) debe registrarse el último, pero el orden es {names}"
        )
