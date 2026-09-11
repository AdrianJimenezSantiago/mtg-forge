"""Tests que garantizan que la interfaz funciona sin conexión a internet.

La app se distribuye como un ``.exe`` local. Cualquier asset que se cargue
desde un CDN convierte "no tengo internet" en "la app está rota". Estos tests
fallan si alguien reintroduce una dependencia de red en el render.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
VENDOR = STATIC / "vendor"

# Dominios que servían assets y que ya no deben aparecer en ningún template.
FORBIDDEN_HOSTS = [
    "cdn.tailwindcss.com",
    "unpkg.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdnjs.cloudflare.com",
]

# Hosts que SÍ pueden aparecer: son APIs de datos o enlaces que el usuario
# abre a mano, no assets que bloqueen el render.
ALLOWED_DATA_HOSTS = [
    "api.scryfall.com", "scryfall.com", "moxfield.com", "archidekt.com",
    "deckstats.net", "tappedout.net", "cubecobra.com", "mtggoldfish.com",
    "drive.google.com", "github.com", "makeplayingcards.com",
    "console.cloud.google.com", "example.com", "s3.amazonaws.com",
]


def _html_files() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def _asset_refs(text: str) -> list[str]:
    """URLs que el navegador cargaría automáticamente (src/href de assets)."""
    pattern = re.compile(
        r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', re.IGNORECASE
    )
    dynamic = re.compile(r"\.src\s*=\s*['\"](https?://[^'\"]+)['\"]")
    return pattern.findall(text) + dynamic.findall(text)


class TestNoCdnAssets:
    @pytest.mark.parametrize("path", _html_files(), ids=lambda p: p.name)
    def test_template_has_no_cdn_asset(self, path):
        text = path.read_text(encoding="utf-8")
        # Se ignoran los comentarios Jinja: el propio código documenta de qué
        # CDNs venía antes, y eso no carga nada.
        without_comments = re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)
        for url in _asset_refs(without_comments):
            host = url.split("/")[2].lower()
            assert not any(bad in host for bad in FORBIDDEN_HOSTS), (
                f"{path.name} carga un asset desde {host}. Vendorízalo en "
                f"static/vendor/ (npm run vendor) — sin internet la app se ve rota."
            )

    def test_app_js_has_no_cdn_asset(self):
        text = (STATIC / "app.js").read_text(encoding="utf-8")
        for url in _asset_refs(text):
            host = url.split("/")[2].lower()
            assert not any(bad in host for bad in FORBIDDEN_HOSTS), host


class TestVendorBundleIsComplete:
    """Los ficheros que los templates referencian tienen que existir."""

    REQUIRED = [
        "tailwind.css",
        "alpine.min.js",
        "alpine-collapse.min.js",
        "alpine-focus.min.js",
        "lucide.min.js",
        "chart.umd.js",
        "mana.min.css",
        "fonts/mana.woff2",
    ]

    @pytest.mark.parametrize("name", REQUIRED)
    def test_asset_exists(self, name):
        path = VENDOR / name
        assert path.exists(), (
            f"Falta static/vendor/{name}. Ejecuta: npm install && npm run vendor "
            f"&& npm run build:css"
        )
        assert path.stat().st_size > 0, f"static/vendor/{name} está vacío"

    def test_every_local_vendor_reference_resolves(self):
        """Ningún template puede apuntar a un /static/... que no exista."""
        pattern = re.compile(r'["\'](/static/[^"\']+)["\']')
        missing = []
        for path in _html_files() + [STATIC / "app.js"]:
            text = path.read_text(encoding="utf-8")
            for ref in pattern.findall(text):
                # Se ignoran las rutas con interpolación de plantilla.
                if "{{" in ref or "{%" in ref or "${" in ref:
                    continue
                target = ROOT / ref.lstrip("/")
                if not target.exists():
                    missing.append(f"{path.name} → {ref}")
        assert not missing, "Referencias rotas a /static:\n" + "\n".join(missing)

    def test_mana_css_only_references_bundled_fonts(self):
        """Todo ``url()`` de mana.min.css debe resolver a un fichero presente.

        El paquete original declara cinco formatos (eot/woff/woff2/ttf/svg) y
        nosotros solo empaquetamos woff2+woff para ahorrar 2,6 MB. Si el
        reescrito del @font-face fallara, el navegador pediría ficheros
        inexistentes en cada carga.

        Se comprueban las URLs, no los nombres: "MPlantin" aparece como
        `font-family` de respaldo en algunas reglas y eso es inofensivo.
        """
        css_path = VENDOR / "mana.min.css"
        # utf-8-sig: el fichero de mana-font viene con BOM.
        css = css_path.read_text(encoding="utf-8-sig")
        refs = re.findall(r'url\(["\']?([^"\')?#]+)', css)
        assert refs, "El @font-face de Mana ha desaparecido del CSS"
        for ref in refs:
            resolved = (css_path.parent / ref).resolve()
            assert resolved.exists(), (
                f"mana.min.css pide {ref} y no está empaquetado. Ejecuta "
                f"npm run vendor."
            )

    def test_tailwind_build_contains_custom_theme(self):
        """El CSS compilado debe incluir el tema propio, no solo el default.

        Si el purgado se pasa de agresivo o el config no se aplica, saldría un
        Tailwind genérico y la app perdería toda su identidad visual sin que
        ningún test lo notara.
        """
        css = (VENDOR / "tailwind.css").read_text(encoding="utf-8")
        # Tailwind emite los colores como `rgb(r g b / alpha)`, no como hex,
        # para poder aplicar opacidad con las variantes `/50`.
        assert ".bg-bg-base{" in css, (
            "Falta la utilidad bg-bg-base — el tema personalizado no se aplicó"
        )
        assert "10 13 19" in css, "Falta el fondo base arcane (#0a0d13)"
        assert "212 175 55" in css, "Falta el dorado del tema (#d4af37)"


class TestTailwindConfig:
    def test_config_file_exists(self):
        assert (ROOT / "tailwind.config.js").exists()

    def test_content_globs_cover_templates_and_js(self):
        cfg = (ROOT / "tailwind.config.js").read_text(encoding="utf-8")
        assert "./templates/**/*.html" in cfg
        assert "./static/js/**/*.js" in cfg, (
            "Los módulos ES extraídos de los templates también generan clases; "
            "sin este glob el purgado se las lleva."
        )
