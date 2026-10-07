from __future__ import annotations

import re

from mpc_forge.services.system import i18n
from mpc_forge.services.system.i18n import _TRANSLATIONS, BASE_LANG, SUPPORTED_LANGS
from tests.frontend._support import all_js, all_templates, read

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_every_language_is_complete_and_consistent():
    assert {code for code, _ in SUPPORTED_LANGS} == set(_TRANSLATIONS)
    base = _TRANSLATIONS[BASE_LANG]
    for lang, table in _TRANSLATIONS.items():
        assert set(table) == set(base), f"'{lang}' difiere: {sorted(set(table) ^ set(base))[:10]}"
        assert not [k for k, v in table.items() if not str(v).strip()], f"Vacías en '{lang}'"
        mismatched = [
            key
            for key, value in table.items()
            if set(PLACEHOLDER.findall(value)) != set(PLACEHOLDER.findall(base[key]))
        ]
        assert not mismatched, f"Placeholders distintos en '{lang}': {mismatched[:10]}"


def test_every_key_used_by_the_interface_exists():
    keys: set[str] = set()
    for path in all_templates():
        text = read(path)
        keys |= set(re.findall(r"\{\{\s*t\.(\w+)\s*(?:\}\}|\|)", text))
        keys |= set(re.findall(r"\bt\[\s*['\"](\w+)['\"]\s*\]", text))
        keys |= set(re.findall(r"window\._T\.(\w+)", text))
    for path in all_js():
        text = read(path)
        keys |= set(re.findall(r"_t\(['\"](\w+)['\"]\)", text))
        keys |= set(re.findall(r"window\._T\.(\w+)", text))
    assert keys, "No se encontró ninguna clave: ¿cambió el patrón de uso?"
    undefined = sorted(keys - set(_TRANSLATIONS[BASE_LANG]))
    assert not undefined, f"Claves usadas pero no definidas: {undefined[:15]}"


def test_missing_keys_fall_back_to_the_base_language(monkeypatch):
    monkeypatch.setitem(_TRANSLATIONS, "xx", {"nav_decks": "Decks-XX"})
    t = i18n.get_translations("xx")
    assert t.nav_decks == "Decks-XX"
    assert t.nav_settings == _TRANSLATIONS[BASE_LANG]["nav_settings"]
    assert i18n.get_translations("en").esta_clave_no_existe == "[esta_clave_no_existe]"
    data = t.as_dict()
    assert set(data) >= set(_TRANSLATIONS[BASE_LANG]) and data["nav_decks"] == "Decks-XX"


def test_js_bundle(monkeypatch):
    body = i18n.bundle_js("en")
    assert all(name in body for name in ("window._LANG", "window._T", "window._t"))
    assert i18n.bundle_js("zz") == i18n.bundle_js(BASE_LANG)
    first = i18n.bundle_version()
    monkeypatch.setattr(i18n, "_VERSION_CACHE", None)
    monkeypatch.setitem(_TRANSLATIONS[BASE_LANG], "nav_decks", "Cambiado")
    assert i18n.bundle_version() != first
