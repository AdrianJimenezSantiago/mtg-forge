"""Orden de ejecución de los scripts en las páginas renderizadas.

Por qué existe este fichero
---------------------------
Al extraer el JavaScript de los templates a módulos ES, la aplicación se rompió
en el navegador con "Alpine Expression Error: importPanel is not defined" y las
vistas en blanco, pese a que la suite estaba entera en verde.

La causa: la build CDN de Alpine termina con
``queueMicrotask(() => Alpine.start())``. Arranca en el microtask inmediatamente
posterior a su propio script diferido. Los `<script defer>` y los
`<script type="module">` comparten un único orden de ejecución — el orden de
aparición en el documento — así que un módulo declarado en el cuerpo de la
página se ejecuta DESPUÉS de que Alpine haya empezado a recorrer el DOM, y en
ese momento las funciones que `x-data` invoca todavía no están en `window`.

Antes no pasaba porque el JavaScript vivía en un `<script>` embebido sin
`defer`, que se ejecutaba durante el parseo, antes que el `defer` de Alpine.

Los tests anteriores no lo detectaron porque comprobaban los módulos AISLADOS
(que cada función existiera y devolviera un objeto) y la estructura de los
templates, pero nunca el ORDEN en que el navegador los ejecuta. Estos tests
cubren justamente eso.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"

# Vistas que renderizan una plantilla completa, con el módulo que cada una debe
# cargar. `proof.html` queda fuera: no usa Alpine.
PAGES = {
    "index.html": "home.js",
    "deck.html": "deck-editor.js",
    "pdf_studio.html": "pdf-studio.js",
    "settings.html": "settings.js",
    "history.html": "history.js",
    "collection.html": "collection.js",
    "print_planner.html": "print-planner.js",
    "art_library.html": "art-library.js",
    "calibration.html": "calibration.js",
}

ALPINE = "alpine.min.js"
API_CLIENT = "api.js"

# Cualquier etiqueta script con src, incluidas las que ocupan varias líneas.
SCRIPT_TAG = re.compile(
    r"<script\b(?P<attrs>[^>]*?)\bsrc=[\"'](?P<src>[^\"']+)[\"'][^>]*>",
    re.DOTALL,
)


def render(template_name: str) -> str:
    """Renderiza la plantilla con el entorno Jinja real de la aplicación.

    Se usa el entorno de verdad, y no una sustitución de texto a mano, para que
    la herencia de plantillas y los bloques se resuelvan igual que en
    producción. Es justo la resolución de bloques la que determina dónde acaba
    cada etiqueta, y por tanto el orden de ejecución.
    """
    import warnings
    warnings.filterwarnings("ignore")
    from mpc_forge.routes.ui import templates
    from mpc_forge.services import i18n

    class _FakeUrl:
        path = "/"

    class _FakeRequest:
        """base.html usa `request.url.path` para marcar el enlace activo."""
        url = _FakeUrl()

    template = templates.env.get_template(template_name)
    # Contexto mínimo: solo interesa la posición de las etiquetas, no los datos.
    return template.render(
        t=i18n.get_translations("es"),
        lang="es",
        request=_FakeRequest(),
        decks=[],
        deck={"id": 1, "name": "test", "cards": []},
        deck_id=1,
        cards=[],
        stats={},
    )


def scripts_in_execution_order(html: str) -> list[str]:
    """Los scripts con `src`, en el orden en que el navegador los ejecuta.

    Regla del estándar: los `defer` clásicos y los módulos (sin `async`)
    comparten una única cola y se ejecutan en orden de documento. Los clásicos
    sin `defer` se ejecutan durante el parseo, es decir antes que toda esa cola.
    Los `async` no tienen orden garantizado y se excluyen.
    """
    parsing_time: list[str] = []
    deferred: list[str] = []

    for match in SCRIPT_TAG.finditer(html):
        attrs = match.group("attrs")
        src = match.group("src").split("?")[0]
        if "async" in attrs:
            continue
        if "defer" in attrs or 'type="module"' in attrs or "type='module'" in attrs:
            deferred.append(src)
        else:
            parsing_time.append(src)

    return parsing_time + deferred


def position(order: list[str], needle: str) -> int:
    for index, src in enumerate(order):
        if src.endswith(needle):
            return index
    return -1


@pytest.fixture(scope="module")
def rendered() -> dict[str, str]:
    return {name: render(name) for name in PAGES}


class TestViewModuleRunsBeforeAlpine:
    """El fallo concreto que se escapó a producción."""

    @pytest.mark.parametrize("page,module", sorted(PAGES.items()), ids=lambda v: v)
    def test_module_executes_before_alpine(self, rendered, page, module):
        order = scripts_in_execution_order(rendered[page])
        module_at = position(order, module)
        alpine_at = position(order, ALPINE)

        assert module_at >= 0, f"{page} no carga /static/js/{module}"
        assert alpine_at >= 0, f"{page} no carga Alpine"
        assert module_at < alpine_at, (
            f"En {page}, {module} se ejecuta DESPUÉS de Alpine "
            f"(posiciones {module_at} y {alpine_at}).\n"
            f"Alpine arranca con queueMicrotask(() => Alpine.start()) nada más "
            f"ejecutarse, así que las funciones de x-data aún no estarán en "
            f"window y la vista saldrá en blanco con "
            f"'Alpine Expression Error: ... is not defined'.\n"
            f"Declara el módulo en {{% block view_module %}}, que base.html "
            f"renderiza en el <head> antes de Alpine.\n"
            f"Orden observado: {order}"
        )

    @pytest.mark.parametrize("page,module", sorted(PAGES.items()), ids=lambda v: v)
    def test_api_client_executes_before_the_view_module(self, rendered, page, module):
        """Los componentes usan `window.api` dentro de su `init()`."""
        order = scripts_in_execution_order(rendered[page])
        api_at = position(order, API_CLIENT)
        module_at = position(order, module)
        assert api_at >= 0, f"{page} no carga el cliente API"
        assert api_at < module_at, (
            f"En {page}, api.js se ejecuta después de {module}. Los "
            f"componentes llaman a window.api en init() y fallarían."
        )

    @pytest.mark.parametrize("page", sorted(PAGES), ids=lambda v: v)
    def test_alpine_plugins_execute_before_alpine_core(self, rendered, page):
        """x-collapse y x-trap quedan inertes, sin error, si llegan tarde."""
        order = scripts_in_execution_order(rendered[page])
        alpine_at = position(order, ALPINE)
        for plugin in ("alpine-collapse.min.js", "alpine-focus.min.js"):
            plugin_at = position(order, plugin)
            assert plugin_at >= 0, f"{page} no carga {plugin}"
            assert plugin_at < alpine_at, (
                f"{plugin} debe registrarse antes que el core de Alpine, o el "
                f"directivo queda inerte sin dar ningún error visible."
            )


class TestRenderedPagesAreCoherent:
    @pytest.mark.parametrize("page", sorted(PAGES), ids=lambda v: v)
    def test_page_renders_without_raising(self, rendered, page):
        assert len(rendered[page]) > 500, f"{page} renderizó casi vacío"

    @pytest.mark.parametrize("page", sorted(PAGES), ids=lambda v: v)
    def test_no_unresolved_jinja_remains(self, rendered, page):
        """Una llave suelta indica un bloque mal cerrado."""
        html = rendered[page]
        assert "{%" not in html, f"{page} deja etiquetas Jinja sin resolver"
        assert "{{" not in html, f"{page} deja interpolaciones sin resolver"

    @pytest.mark.parametrize("page", sorted(PAGES), ids=lambda v: v)
    def test_every_script_src_is_local(self, rendered, page):
        for match in SCRIPT_TAG.finditer(rendered[page]):
            src = match.group("src")
            assert not src.startswith("http"), (
                f"{page} carga un script remoto: {src}"
            )

    @pytest.mark.parametrize("page,module", sorted(PAGES.items()), ids=lambda v: v)
    def test_x_data_symbols_are_defined_somewhere_on_the_page(
        self, rendered, page, module
    ):
        """Cruce entre lo que el HTML invoca y lo que la página define.

        Se mira el HTML FINAL, no la plantilla suelta, porque una vista hereda
        de base.html: el sidebar aporta sus propios componentes
        (`sidebarData`, `globalCardSearch`) desde un script embebido que no se
        extrajo. Comparar solo contra el módulo de la vista daría falsos
        positivos.
        """
        html = rendered[page]

        # 1) Lo que publica el módulo de la vista.
        js = (ROOT / "static" / "js" / module).read_text(encoding="utf-8")
        exposed = set(re.findall(r"^window\.([\w$]+)\s*=", js, re.MULTILINE))
        # api.js también publica en window, y algún x-data podría usarlo.
        api_js = (ROOT / "static" / "js" / "api.js").read_text(encoding="utf-8")
        exposed |= set(re.findall(r"^window\.([\w$]+)\s*=", api_js, re.MULTILINE))

        # 2) Lo que declaran los scripts embebidos que quedan (base.html).
        for inline in re.finditer(
            r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html
        ):
            body = inline.group(1)
            # El cuerpo del script viene indentado dentro del HTML, así que
            # las declaraciones de nivel superior NO empiezan en la columna 0.
            exposed |= set(re.findall(
                r"^[ \t]*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                body, re.MULTILINE,
            ))
            exposed |= set(re.findall(
                r"^[ \t]*window\.([\w$]+)\s*=", body, re.MULTILINE
            ))

        referenced = set()
        for match in re.finditer(r'x-data="([^"]*)"', html):
            referenced.update(
                re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", match.group(1))
            )

        missing = referenced - exposed
        assert not missing, (
            f"{page} invoca {sorted(missing)} en x-data y nada en la página los "
            f"define. Alpine no los encontrará y esa parte de la vista quedará "
            f"en blanco."
        )


class TestBaseTemplateContract:
    """El bloque `view_module` es lo que garantiza el orden. Si desaparece o se
    mueve por debajo de Alpine, todo lo anterior vuelve a romperse."""

    @pytest.fixture(scope="class")
    def base(self) -> str:
        return (TEMPLATES / "base.html").read_text(encoding="utf-8")

    def test_block_exists(self, base):
        assert "{% block view_module %}" in base, (
            "base.html ya no define el bloque view_module. Las plantillas hijas "
            "no tendrán dónde declarar su módulo."
        )

    def test_block_is_declared_before_alpine(self, base):
        block_at = base.index("{% block view_module %}")
        alpine_at = base.index("vendor/alpine.min.js")
        assert block_at < alpine_at, (
            "El bloque view_module está por debajo de Alpine en base.html. "
            "Los módulos de las vistas se ejecutarían tarde."
        )

    def test_api_client_is_declared_before_the_block(self, base):
        api_at = base.index("js/api.js")
        block_at = base.index("{% block view_module %}")
        assert api_at < block_at

    @pytest.mark.parametrize("page", sorted(PAGES), ids=lambda v: v)
    def test_child_declares_the_block(self, page):
        html = (TEMPLATES / page).read_text(encoding="utf-8")
        assert "{% block view_module %}" in html, (
            f"{page} no declara su módulo en el bloque view_module"
        )
