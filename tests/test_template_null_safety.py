from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
JS_DIR = ROOT / "static" / "js"

VIEWS = {
    "print_planner.html": "print-planner.js",
    "art_library.html": "art-library.js",
    "calibration.html": "calibration.js",
    "deck.html": "deck-editor.js",
    "settings.html": "settings.js",
    "history.html": "history.js",
    "collection.html": "collection.js",
    "index.html": "home.js",
    "landing.html": "landing.js",
}

EAGER_ATTRS = (
    "x-text",
    "x-html",
    "x-model",
    ":class",
    ":style",
    ":href",
    ":src",
    ":value",
    ":disabled",
    "x-show",
)

NULL_PROP = re.compile(r"^\s{4}([A-Za-z_$][\w$]*)\s*:\s*null\s*,", re.MULTILINE)


def nullable_properties(module: str) -> set[str]:
    source = (JS_DIR / module).read_text(encoding="utf-8")
    return set(NULL_PROP.findall(source))


def guarded_regions(html: str, prop: str) -> list[tuple[int, int]]:
    regions = []
    pattern = re.compile(r'<template\s+x-if="[^"]*\b' + re.escape(prop) + r'\b[^"]*"', re.DOTALL)
    for match in pattern.finditer(html):
        depth = 0
        position = match.start()
        for tag in re.finditer(r"</?template\b", html[match.start() :]):
            if tag.group().startswith("</"):
                depth -= 1
                if depth == 0:
                    position = match.start() + tag.end()
                    break
            else:
                depth += 1
        regions.append((match.start(), position))
    return regions


def strip_jinja_comments(html: str) -> str:
    return re.sub(r"\{#.*?#\}", "", html, flags=re.DOTALL)


def unsafe_dereferences(html: str, prop: str) -> list[str]:
    html = strip_jinja_comments(html)
    guarded = guarded_regions(html, prop)
    offenders = []

    attr_pattern = re.compile(r"(" + "|".join(re.escape(a) for a in EAGER_ATTRS) + r')="([^"]*)"')
    deref = re.compile(r"\b" + re.escape(prop) + r"\.")

    for match in attr_pattern.finditer(html):
        expression = match.group(2)
        if not deref.search(expression):
            continue
        if re.search(r"\b" + re.escape(prop) + r"\?\.", expression):
            continue
        inline_guard = re.compile(r"\b" + re.escape(prop) + r"\b\s*(&&|\?[^.])")
        if inline_guard.search(expression):
            continue
        if any(start <= match.start() <= end for start, end in guarded):
            continue
        offenders.append(f'{match.group(1)}="{expression[:80]}"')

    return offenders


@pytest.mark.parametrize("template,module", sorted(VIEWS.items()), ids=lambda v: v)
def test_no_unguarded_null_dereference(template, module):
    path = TEMPLATES / template
    if not path.exists() or not (JS_DIR / module).exists():
        pytest.skip(f"{template} o {module} no existen")

    html = path.read_text(encoding="utf-8")
    problems: list[str] = []

    for prop in sorted(nullable_properties(module)):
        if prop.startswith("_"):
            continue
        for offender in unsafe_dereferences(html, prop):
            problems.append(f"  {prop}: {offender}")

    assert not problems, (
        f"{template} desreferencia estado que arranca en null sin protegerlo:\n"
        + "\n".join(problems)
        + "\n\nx-show NO protege: Alpine evalúa las expresiones aunque el "
        'elemento esté oculto. Usa <template x-if="prop"> (que no crea el '
        "DOM) o encadenamiento opcional (prop?.campo)."
    )


class TestTheGuardItself:
    def test_flags_a_bare_dereference(self):
        html = '<div x-text="fmt(result.x)"></div>'
        assert unsafe_dereferences(html, "result")

    def test_accepts_optional_chaining(self):
        html = '<div x-text="fmt(result?.x)"></div>'
        assert not unsafe_dereferences(html, "result")

    def test_accepts_a_template_if_guard(self):
        html = '<template x-if="result"><div x-text="fmt(result.x)"></div></template>'
        assert not unsafe_dereferences(html, "result")

    def test_x_show_is_not_accepted_as_a_guard(self):
        html = '<section x-show="result"><div x-text="fmt(result.x)"></div></section>'
        assert unsafe_dereferences(html, "result"), "x-show no debe contar como protección"

    def test_ignores_properties_that_are_not_dereferenced(self):
        html = '<div x-show="result"></div>'
        assert not unsafe_dereferences(html, "result")

    def test_accepts_an_inline_and_guard(self):
        html = '<div x-text="result && result.x"></div>'
        assert not unsafe_dereferences(html, "result")

    def test_accepts_an_inline_ternary_guard(self):
        html = """<div x-text="deck ? deck.name : \'\'"></div>"""
        assert not unsafe_dereferences(html, "deck")

    def test_ignores_examples_inside_jinja_comments(self):
        html = '{# mal: x-text="fmt(result.x)" #}<div x-text="ok"></div>'
        assert not unsafe_dereferences(html, "result")

    def test_detects_nullable_properties_in_a_module(self, tmp_path):
        module = tmp_path / "m.js"
        module.write_text(
            "function view() {\n"
            "  return {\n"
            "    result: null,\n"
            "    plan: null,\n"
            "    items: [],\n"
            "    loading: true,\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        found = set(NULL_PROP.findall(module.read_text(encoding="utf-8")))
        assert found == {"result", "plan"}
