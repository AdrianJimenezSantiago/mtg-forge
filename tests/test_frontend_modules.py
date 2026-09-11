"""Tests de la modularización del frontend (ítems 12 y 13).

No ejecutan JavaScript — eso lo hace `npm run test:js` con jsdom, que corre en
CI. Lo que se comprueba aquí es el contrato estructural entre los templates y
los módulos, que es donde se rompen las cosas al editar HTML:

* ningún template puede volver a acumular JavaScript embebido
* toda función referenciada desde `x-data` debe publicarse en `window`
* todo `<script src>` debe apuntar a un fichero que exista
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
JS_DIR = ROOT / "static" / "js"

# Templates que deben tener su JS en un módulo aparte. `base.html` está
# excluido a propósito: sus bloques son configuración de arranque diminuta y
# moverlos añadiría una petición en la ruta crítica de cada página.
MODULARIZED = {
    "deck.html": "deck-editor.js",
    "pdf_studio.html": "pdf-studio.js",
    "settings.html": "settings.js",
    "index.html": "home.js",
    "history.html": "history.js",
    "collection.html": "collection.js",
    # Vista nueva del Sprint 4: nace ya como módulo, nunca tuvo JS embebido.
    "print_planner.html": "print-planner.js",
    "art_library.html": "art-library.js",
    "calibration.html": "calibration.js",
}

INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)</script>")
X_DATA_RE = re.compile(r'x-data="([^"]*)"')
WINDOW_EXPORT_RE = re.compile(r"^window\.([\w$]+)\s*=", re.MULTILINE)

# Umbral de JS embebido tolerado por template. base.html tiene ~90 líneas de
# arranque; el resto debe estar a cero. Sin un tope explícito, el JS vuelve a
# colarse en el HTML poco a poco y en un año estamos igual que al principio.
MAX_INLINE_JS_LINES = 5


def _inline_js(template: str) -> str:
    html = (TEMPLATES / template).read_text(encoding="utf-8")
    return "\n".join(m.group(2) for m in INLINE_SCRIPT_RE.finditer(html))


class TestNoInlineJavaScript:
    @pytest.mark.parametrize("template", sorted(MODULARIZED), ids=lambda t: t)
    def test_template_has_no_inline_script(self, template):
        js = _inline_js(template)
        lines = [ln for ln in js.splitlines() if ln.strip()]
        assert len(lines) <= MAX_INLINE_JS_LINES, (
            f"{template} ha vuelto a acumular {len(lines)} líneas de JS "
            f"embebido. Muévelas a static/js/ y ejecuta "
            f"`python scripts/extract_inline_js.py`."
        )

    @pytest.mark.parametrize("template", sorted(MODULARIZED), ids=lambda t: t)
    def test_template_loads_its_module(self, template):
        html = (TEMPLATES / template).read_text(encoding="utf-8")
        module = MODULARIZED[template]
        # El src lleva `?v={{ asset_v(...) }}` para invalidar la caché del
        # navegador cuando se actualiza la app, así que se busca el prefijo.
        assert f'src="/static/js/{module}' in html, (
            f"{template} no carga /static/js/{module}"
        )
        assert f"asset_v('js/{module}')" in html, (
            f"{template} carga {module} sin cache-busting: tras actualizar la "
            f"app, el navegador seguiría ejecutando la versión antigua hasta "
            f"un día entero."
        )
        assert "defer" in html, (
            f"{template} debe cargar su script con `defer`. No se usa "
            f"type=\"module\" a propósito: ver la nota en static/js/api.js."
        )
        assert "{% block view_module %}" in html, (
            f"{template} debe declarar su script en el bloque view_module, que "
            f"base.html renderiza antes de Alpine. Si se declara en el cuerpo, "
            f"se ejecuta tarde y la vista sale en blanco."
        )


class TestModulesExist:
    @pytest.mark.parametrize("module", sorted(set(MODULARIZED.values())))
    def test_module_file_exists_and_is_not_empty(self, module):
        path = JS_DIR / module
        assert path.exists(), f"Falta static/js/{module}"
        assert path.stat().st_size > 100, f"static/js/{module} está casi vacío"

    def test_api_client_exists(self):
        assert (JS_DIR / "api.js").exists()

    def test_every_script_src_resolves(self):
        """Un `src` roto no da error visible: la vista simplemente no arranca."""
        pattern = re.compile(r'<script[^>]*\bsrc="(/static/[^"]+)"', re.DOTALL)
        missing = []
        for template in sorted(TEMPLATES.glob("*.html")):
            html = template.read_text(encoding="utf-8")
            for ref in pattern.findall(html):
                # Los assets llevan `?v={{ asset_v(...) }}` para invalidar la
                # caché del navegador; se descarta la query antes de resolver.
                path = ref.split("?")[0]
                if not (ROOT / path.lstrip("/")).exists():
                    missing.append(f"{template.name} → {path}")
        assert not missing, "Scripts que no existen:\n" + "\n".join(missing)


class TestAlpineBridge:
    """Alpine evalúa `x-data` en el ámbito global.

    Un módulo ES tiene ámbito propio, así que una función solo declarada
    dentro del módulo es invisible para Alpine: la vista se queda en blanco sin
    ningún error en consola. Estos tests atrapan justo eso.
    """

    @pytest.mark.parametrize("template", sorted(MODULARIZED), ids=lambda t: t)
    def test_every_x_data_symbol_is_exposed(self, template):
        html = (TEMPLATES / template).read_text(encoding="utf-8")
        js = (JS_DIR / MODULARIZED[template]).read_text(encoding="utf-8")
        exposed = set(WINDOW_EXPORT_RE.findall(js))

        referenced = set()
        for match in X_DATA_RE.finditer(html):
            referenced.update(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", match.group(1)))

        missing = referenced - exposed
        assert not missing, (
            f"{template} usa {sorted(missing)} en x-data pero "
            f"{MODULARIZED[template]} no las publica en window. Alpine no las "
            f"encontrará y la vista no arrancará."
        )

    @pytest.mark.parametrize("template,module", sorted(MODULARIZED.items()))
    def test_module_says_which_view_it_serves(self, template, module):
        """Cada módulo debe decir a qué vista pertenece.

        Los que salieron de una extracción indican además el template de origen
        y cómo regenerarlos. Los que nacieron ya como fichero aparte solo
        necesitan nombrar su ruta: no hay nada que regenerar.
        """
        content = (JS_DIR / module).read_text(encoding="utf-8")
        header = content[:2000]

        extracted = "extract_inline_js" in header
        if extracted:
            assert f"templates/{template}" in header or "templates/" in header, (
                f"{module} salió de una extracción pero no dice de qué template"
            )
        else:
            route_hint = "/" + template.replace(".html", "").replace("_", "-")
            assert route_hint in header or template in header, (
                f"{module} no indica a qué vista pertenece. Añade la ruta "
                f"({route_hint}) o el template ({template}) en la cabecera."
            )


class TestApiClientSurface:
    """El cliente centralizado debe cubrir los dominios reales de la API."""

    @pytest.fixture(scope="class")
    def api_js(self) -> str:
        return (JS_DIR / "api.js").read_text(encoding="utf-8")

    @pytest.mark.parametrize("domain", [
        "decks", "cards", "preload", "build", "cardback", "drives",
        "sources", "customArt", "collection", "settings", "bulk",
        "thumbs", "runs", "backup", "autofill",
    ])
    def test_domain_is_present(self, api_js, domain):
        assert re.search(rf"^\s+{domain}:\s*\{{", api_js, re.MULTILINE), (
            f"api.js no define el grupo '{domain}'"
        )

    def test_defines_error_class(self, api_js):
        assert "class ApiError" in api_js
        assert "window.ApiError" in api_js, (
            "ApiError debe publicarse en window: api.js es un script clásico y "
            "el resto de la interfaz lo consume desde ahí."
        )

    def test_defines_poll_helper(self, api_js):
        assert "async function poll" in api_js
        assert "window.apiPoll" in api_js

    def test_is_a_classic_script_not_a_module(self, api_js):
        """Un `export` obligaría a type="module" y reintroduciría el fallo de
        orden que dejó las vistas en blanco. Ver la nota en api.js."""
        import re
        assert not re.search(r"^export\s", api_js, re.MULTILINE), (
            "api.js ha vuelto a usar `export`, lo que exige type=\"module\" y "
            "cambia el momento en que se ejecuta respecto a Alpine."
        )

    def test_handles_fastapi_validation_arrays(self, api_js):
        """Los 422 traen `detail` como lista de objetos.

        Sin tratarlos, el usuario veía "[object Object]" como mensaje de error.
        """
        assert "Array.isArray(detail)" in api_js

    def test_treats_204_as_empty(self, api_js):
        """`res.json()` sobre un 204 lanza. Varios DELETE devuelven 204."""
        assert "204" in api_js

    def test_propagates_abort_errors(self, api_js):
        """Cancelar no es fallar: no debe convertirse en un toast de error."""
        assert "AbortError" in api_js

    def test_prints_helper_accepts_a_signal(self, api_js):
        """El selector de arte tiene que poder cortar sus peticiones al cerrar."""
        prints = api_js[api_js.index("prints:"):]
        prints = prints[:prints.index("},")]
        assert "signal" in prints


class TestExtractionScript:
    def test_script_exists(self):
        assert (ROOT / "scripts" / "extract_inline_js.py").exists()

    def test_extraction_is_up_to_date(self):
        """Si alguien edita un módulo pero no el template (o al revés), esto
        lo detecta antes que el usuario."""
        import subprocess
        result = subprocess.run(
            ["python", str(ROOT / "scripts" / "extract_inline_js.py"), "--check"],
            capture_output=True, text=True, cwd=ROOT,
        )
        assert result.returncode == 0, (
            f"La extracción está desfasada:\n{result.stdout}"
        )
