from __future__ import annotations

import re

from tests.frontend._support import (
    JS,
    ROOT,
    VENDOR,
    all_js,
    all_templates,
    needs_node,
    read,
    run_js_test,
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


def _call_arguments(text: str, start: int) -> list[str]:
    """Argumentos de primer nivel de la llamada cuyo "(" está en `start`.

    Entiende cadenas, template literals con ${...} anidados y paréntesis, que
    es todo lo que aparece en los argumentos de los toasts.
    """
    args: list[str] = []
    current: list[str] = []
    stack: list[list] = [["code", 0]]
    i = start + 1
    while i < len(text):
        ch = text[i]
        ctx = stack[-1]
        if ctx[0] in "'\"`":
            if ch == "\\":
                current.append(text[i : i + 2])
                i += 2
                continue
            if ctx[0] == "`" and text.startswith("${", i):
                stack.append(["code", 0])
                current.append("${")
                i += 2
                continue
            if ch == ctx[0]:
                stack.pop()
        elif ch in "'\"`":
            stack.append([ch])
        elif ch in "([{":
            ctx[1] += 1
        elif ch in ")]}":
            if ctx[1] == 0:
                if len(stack) == 1:
                    args.append("".join(current).strip())
                    return [a for a in args if a]
                stack.pop()
            else:
                ctx[1] -= 1
        elif ch == "," and len(stack) == 1 and ctx[1] == 0:
            args.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    raise AssertionError("llamada sin cerrar")


def test_every_toast_says_what_kind_it_is():
    kinds = re.compile(r"^(?:'(?:info|success|warning|error)'|.+\?\s*'\w+'\s*:\s*'\w+')$", re.S)
    offenders = []
    for path in all_js():
        text = read(path)
        for match in re.finditer(r"window\.toast\(", text):
            if text[: match.start()].endswith("window.toast = "):
                continue
            args = _call_arguments(text, match.end() - 1)
            line = text.count("\n", 0, match.start()) + 1
            if len(args) != 3 or not kinds.match(args[2]):
                offenders.append(f"{path.name}:{line} {args}")
    assert not offenders, "\n".join(offenders)


@needs_node
def test_save_file_asks_where_before_generating():
    result, report = run_js_test("save_file.mjs", "core/app.js")
    assert result.returncode == 0, result.stderr
    assert report == {"problems": []}


@needs_node
class TestPdfStudioPreview:
    def test_every_card_sits_on_a_cut_guide(self):
        result, report = run_js_test("pdf_preview_alignment.mjs", "pages/pdf-studio.js")
        assert result.returncode == 0, result.stderr
        assert report["cases"] == 48 and report["misaligned"] == [], report["misaligned"][:3]

    def test_press_mode_follows_the_print_shop_template(self):
        result, report = run_js_test("pdf_press_preview.mjs", "pages/pdf-studio.js")
        assert result.returncode == 0, result.stderr
        assert report == {"pages": 6, "problems": []}, report


def test_downloads_go_through_save_file():
    offenders = []
    for path in all_js():
        for match in re.finditer(r"window\.location(?:\.href)?\s*=\s*[`'\"]/api/", read(path)):
            offenders.append(f"{path.name}: {match.group(0)}")
    for path in all_templates():
        for tag in re.findall(r"<a\b[^>]*>", read(path), re.S):
            if "/api/" not in tag or "href" not in tag:
                continue
            if "@click.prevent" not in tag and 'target="_blank"' not in tag:
                offenders.append(f"{path.name}: {' '.join(tag.split())[:120]}")
    assert not offenders, "\n".join(offenders)


@needs_node
def test_batch_custom_art_uploads_only_what_the_user_confirmed():
    result, report = run_js_test("custom_art_batch.mjs", "core/custom-art-batch.js")
    assert result.returncode == 0, result.stderr
    assert report == {"problems": []}, report


@needs_node
def test_deck_tools_compute_odds_and_shortcuts():
    result, report = run_js_test("deck_tools.mjs", "pages/deck-tools.js")
    assert result.returncode == 0, result.stderr
    assert report == {"problems": []}, report
