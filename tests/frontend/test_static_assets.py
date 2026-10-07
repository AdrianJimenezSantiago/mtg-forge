from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tests.frontend._support import (
    JS,
    ROOT,
    STATIC,
    TEMPLATES,
    VENDOR,
    all_js,
    all_templates,
    read,
)

FORBIDDEN_HOSTS = (
    "cdn.tailwindcss.com",
    "unpkg.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdnjs.cloudflare.com",
)
VENDOR_FILES = (
    "tailwind.css",
    "alpine.min.js",
    "alpine-collapse.min.js",
    "alpine-focus.min.js",
    "auto-animate.min.js",
    "lucide.min.js",
    "mana.min.css",
    "fonts/mana.woff2",
)
API_DOMAINS = (
    "decks",
    "cards",
    "preload",
    "build",
    "cardback",
    "drives",
    "sources",
    "customArt",
    "collection",
    "settings",
    "bulk",
    "thumbs",
    "runs",
    "backup",
    "autofill",
)
PAGINATED_ENDPOINTS = ("/prints", "/api/library/browse")


def _remote_asset_urls(text: str) -> list[str]:
    attr = re.compile(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', re.IGNORECASE)
    dynamic = re.compile(r"\.src\s*=\s*['\"](https?://[^'\"]+)['\"]")
    return attr.findall(text) + dynamic.findall(text)


class TestOfflineBundle:
    def test_vendor_files_are_bundled(self):
        missing = [
            n for n in VENDOR_FILES if not (VENDOR / n).is_file() or not (VENDOR / n).stat().st_size
        ]
        assert not missing, f"Faltan assets: {missing}. Ejecuta npm run vendor && npm run build:css"
        assert not (VENDOR / "chart.umd.js").exists(), "Chart.js no se usa: no debe empaquetarse"

    def test_nothing_is_loaded_from_a_cdn(self):
        offenders = []
        for path in [*all_templates(), *all_js()]:
            for url in _remote_asset_urls(read(path)):
                host = url.split("/")[2].lower()
                if any(bad in host for bad in FORBIDDEN_HOSTS):
                    offenders.append(f"{path.relative_to(ROOT)} → {host}")
        assert not offenders, "\n".join(offenders)

    def test_every_static_reference_resolves(self):
        pattern = re.compile(r'["\'](/static/[^"\'?]+)')
        missing = []
        for path in [*all_templates(), *all_js()]:
            for ref in pattern.findall(read(path)):
                dynamic = any(token in ref for token in ("{{", "{%", "${"))
                if not dynamic and not (ROOT / ref.lstrip("/")).exists():
                    missing.append(f"{path.relative_to(ROOT)} → {ref}")
        assert not missing, "\n".join(missing)

    def test_stylesheets_reference_bundled_files(self):
        css = (VENDOR / "mana.min.css").read_text(encoding="utf-8-sig")
        refs = re.findall(r'url\(["\']?([^"\')?#]+)', css)
        assert refs and all((VENDOR / ref).resolve().exists() for ref in refs)

        tailwind = read(VENDOR / "tailwind.css")
        assert ".bg-bg-base{" in tailwind and "10 13 19" in tailwind and "212 175 55" in tailwind
        config = read(ROOT / "tailwind.config.js")
        assert "./templates/**/*.html" in config and "./static/js/**/*.js" in config


class TestLucideSubset:
    def test_every_static_icon_is_in_the_bundle(self):
        bundle = read(VENDOR / "lucide.min.js")
        names = set()
        for path in all_templates():
            names |= set(re.findall(r'\bdata-lucide="([a-z0-9-]+)"', read(path)))
        for path in all_js():
            names |= set(re.findall(r"\bicon:\s*'([a-z0-9-]+)'", read(path)))
        assert names, "No se encontró ningún icono: ¿cambió el patrón?"

        def pascal(name: str) -> str:
            return "".join(part.capitalize() for part in re.split(r"[-_]", name))

        missing = sorted(n for n in names if not re.search(rf"\b{pascal(n)}\b", bundle))
        assert not missing, f"Iconos usados que no están en el bundle: {missing}. npm run vendor"

    def test_bundle_only_replaces_placeholders(self):
        bundle = read(VENDOR / "lucide.min.js")
        assert "[data-lucide]:not(svg)" in bundle
        assert len(bundle) < 120_000, "El bundle de Lucide ha vuelto a incluir todos los iconos"


class TestApiClient:
    def test_surface(self):
        api = read(JS / "core/api.js")
        missing = [d for d in API_DOMAINS if not re.search(rf"^\s+{d}:\s*\{{", api, re.MULTILINE)]
        assert not missing, f"api.js no define: {missing}"
        assert "class ApiError" in api and "window.ApiError" in api
        assert "async function poll" in api and "window.apiPoll" in api
        assert not re.search(r"^export\s", api, re.MULTILINE), "api.js debe ser un script clásico"
        assert "Array.isArray(detail)" in api and "204" in api and "AbortError" in api
        prints = api[api.index("prints:") :]
        assert "signal" in prints[: prints.index("},")]

    def test_all_prints_helper_pages_through_results(self):
        api = read(JS / "core/api.js")
        body = api[api.index("allPrints:") :]
        body = body[: body.index("\n    },")]
        assert body.index("first.custom") < body.index("first.items")
        assert "has_more" in body and "maxPages" in body
        for module, function in (
            ("pages/pdf-studio.js", "openMiniArtPicker"),
            ("pages/deck-editor.js", "selectCard"),
        ):
            source = read(JS / module)
            assert function in source and "allPrints" in source, module

    def test_paginated_endpoints_are_never_fetched_raw(self):
        offenders = []
        for path in all_js():
            if path.name == "api.js":
                continue
            for call in re.findall(r"fetch\([^)]*\)", read(path), re.DOTALL):
                if any(endpoint in call for endpoint in PAGINATED_ENDPOINTS):
                    offenders.append(f"{path.name}: {call[:90]}")
        assert not offenders, "Usa api.cards.allPrints()/api.request:\n" + "\n".join(offenders)

    @pytest.mark.skipif(shutil.which("node") is None, reason="necesita node")
    def test_pdf_preview_places_every_card_on_a_cut_guide(self):
        result = subprocess.run(
            [
                shutil.which("node"),
                str(ROOT / "tests" / "js" / "pdf_preview_alignment.mjs"),
                str(JS / "pages/pdf-studio.js"),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])
        assert report["cases"] == 48 and report["misaligned"] == [], report["misaligned"][:3]


def test_assets_and_templates_are_organised_by_kind():
    assert [p for p in STATIC.iterdir() if p.is_file()] == []
    assert {p.name for p in JS.iterdir()} == {"core", "pages"}
    assert {p.suffix for p in (STATIC / "css").iterdir()} == {".css"}
    assert [p.name for p in TEMPLATES.glob("*.html")] == ["base.html"]
