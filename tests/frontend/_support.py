from __future__ import annotations

import re
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
JS = STATIC / "js"
VENDOR = STATIC / "vendor"

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

PAGE_MODULES = {
    "pages/deck_library.html": "pages/home.js",
    "pages/landing.html": "pages/landing.js",
    "pages/deck.html": "pages/deck-editor.js",
    "pages/pdf_studio.html": "pages/pdf-studio.js",
    "pages/settings.html": "pages/settings.js",
    "pages/history.html": "pages/history.js",
    "pages/collection.html": "pages/collection.js",
    "pages/print_planner.html": "pages/print-planner.js",
    "pages/art_library.html": "pages/art-library.js",
    "pages/calibration.html": "pages/calibration.js",
    "pages/proof.html": "pages/proof.js",
}

SCRIPT_TAG = re.compile(
    r"<script\b(?P<attrs>[^>]*?)\bsrc=[\"'](?P<src>[^\"']+)[\"'][^>]*>",
    re.DOTALL,
)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>")
WINDOW_EXPORT = re.compile(r"^window\.([\w$]+)\s*=", re.MULTILINE)
INCLUDE = re.compile(r'\{%\s*include\s+"([^"]+)"\s*%\}')


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def template_source(name: str) -> str:
    return INCLUDE.sub(lambda m: template_source(m.group(1)), read(TEMPLATES / name))


def all_templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def all_js() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def render(name: str) -> str:
    warnings.filterwarnings("ignore")
    from mpc_forge.routes.ui import templates
    from mpc_forge.services.system import i18n

    class _Url:
        path = "/"

    class _Request:
        url = _Url()

    return templates.env.get_template(name).render(
        t=i18n.get_translations("es"),
        lang="es",
        request=_Request(),
        decks=[],
        deck={"id": 1, "name": "test", "cards": []},
        hand=[],
        workshop={},
        site_names=[],
        missing_kind="page",
        missing_path="/x",
    )


def scripts_in_execution_order(html: str) -> list[str]:
    parsing, deferred = [], []
    for match in SCRIPT_TAG.finditer(html):
        attrs, src = match.group("attrs"), match.group("src").split("?")[0]
        if "async" in attrs:
            continue
        (deferred if "defer" in attrs or "module" in attrs else parsing).append(src)
    return parsing + deferred


def position(order: list[str], suffix: str) -> int:
    return next((i for i, src in enumerate(order) if src.endswith(suffix)), -1)
