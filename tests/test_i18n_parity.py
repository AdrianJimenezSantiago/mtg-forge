"""Paridad y salud del registro de traducciones.

Por qué existe
--------------
El registro vive en un único diccionario Python de ~1.500 líneas. Nada impedía
añadir una clave en español y olvidarla en inglés: la interfaz en inglés
mostraba entonces el marcador ``[deck_confirmdelete_msg]`` en mitad de un
diálogo de confirmación. La paridad estaba bien por disciplina, no por
construcción.

Estos tests la convierten en algo que CI garantiza.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mpc_forge.services import i18n
from mpc_forge.services.i18n import _TRANSLATIONS, BASE_LANG, SUPPORTED_LANGS

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestKeyParity:
    def test_every_language_has_the_same_keys(self):
        base_keys = set(_TRANSLATIONS[BASE_LANG])
        for lang, data in _TRANSLATIONS.items():
            if lang == BASE_LANG:
                continue
            missing = base_keys - set(data)
            extra = set(data) - base_keys
            assert not missing, (
                f"Faltan {len(missing)} claves en '{lang}': {sorted(missing)[:10]}"
            )
            assert not extra, (
                f"'{lang}' tiene {len(extra)} claves que no existen en "
                f"'{BASE_LANG}': {sorted(extra)[:10]}"
            )

    def test_supported_langs_matches_the_registry(self):
        """El selector de idioma no debe ofrecer idiomas sin traducciones."""
        declared = {code for code, _label in SUPPORTED_LANGS}
        assert declared == set(_TRANSLATIONS)

    def test_no_empty_values(self):
        for lang, data in _TRANSLATIONS.items():
            empty = [k for k, v in data.items() if not str(v).strip()]
            assert not empty, f"Claves vacías en '{lang}': {empty}"

    @pytest.mark.parametrize("lang", sorted(_TRANSLATIONS))
    def test_placeholders_match_across_languages(self, lang):
        """Una traducción no puede perder ni inventar un ``{placeholder}``.

        ``"Added {n} card(s)"`` traducido sin el ``{n}`` deja al usuario sin el
        número, y uno inventado revienta el ``.format()`` en tiempo de
        ejecución. Es el fallo de traducción más caro y el más fácil de
        detectar automáticamente.
        """
        pattern = re.compile(r"\{(\w+)\}")
        base = _TRANSLATIONS[BASE_LANG]
        for key, value in _TRANSLATIONS[lang].items():
            expected = set(pattern.findall(base.get(key, "")))
            actual = set(pattern.findall(value))
            assert expected == actual, (
                f"Los placeholders de '{key}' difieren entre "
                f"'{BASE_LANG}' ({sorted(expected)}) y '{lang}' ({sorted(actual)})"
            )


class TestFallbackChain:
    def test_missing_key_falls_back_to_base_language(self, monkeypatch):
        """Una clave ausente devuelve el idioma base, no ``[clave]``."""
        monkeypatch.setitem(
            _TRANSLATIONS, "xx", {"nav_decks": "Decks-XX"}
        )
        t = i18n.get_translations("xx")
        assert t.nav_decks == "Decks-XX"
        # `nav_settings` no existe en 'xx' → cae al español, no a "[nav_settings]".
        assert t.nav_settings == _TRANSLATIONS[BASE_LANG]["nav_settings"]

    def test_unknown_key_still_returns_the_marker(self):
        """Si la clave no existe en NINGÚN idioma, sigue siendo un error visible."""
        t = i18n.get_translations("en")
        assert t.esta_clave_no_existe == "[esta_clave_no_existe]"

    def test_as_dict_resolves_the_fallback(self, monkeypatch):
        monkeypatch.setitem(_TRANSLATIONS, "xx", {"nav_decks": "Decks-XX"})
        data = i18n.get_translations("xx").as_dict()
        # El dict que va al navegador lleva TODAS las claves ya resueltas, para
        # que el JS nunca reciba un undefined.
        assert set(data) >= set(_TRANSLATIONS[BASE_LANG])
        assert data["nav_decks"] == "Decks-XX"


class TestUsedKeysExist:
    """Ninguna plantilla ni módulo JS debe referenciar una clave inexistente."""

    def _referenced_keys(self) -> set[str]:
        keys: set[str] = set()
        for path in (PROJECT_ROOT / "templates").glob("*.html"):
            keys |= set(re.findall(r"\{\{\s*t\.(\w+)\s*\}\}", path.read_text(encoding="utf-8")))
        for path in (PROJECT_ROOT / "static" / "js").glob("*.js"):
            text = path.read_text(encoding="utf-8")
            keys |= set(re.findall(r"_t\(['\"](\w+)['\"]\)", text))
            keys |= set(re.findall(r"window\._T\.(\w+)", text))
        return keys

    def test_every_referenced_key_is_defined(self):
        referenced = self._referenced_keys()
        assert referenced, "No se encontró ninguna clave: ¿cambió el patrón de uso?"
        undefined = sorted(referenced - set(_TRANSLATIONS[BASE_LANG]))
        assert not undefined, (
            f"{len(undefined)} claves usadas en la interfaz pero no definidas: "
            f"{undefined[:15]}"
        )


class TestJsBundle:
    def test_version_changes_with_content(self, monkeypatch):
        first = i18n.bundle_version()
        monkeypatch.setattr(i18n, "_VERSION_CACHE", None)
        monkeypatch.setitem(_TRANSLATIONS[BASE_LANG], "nav_decks", "Cambiado")
        assert i18n.bundle_version() != first

    def test_bundle_defines_the_expected_globals(self):
        body = i18n.bundle_js("en")
        assert "window._LANG" in body
        assert "window._T" in body
        assert "window._t" in body

    def test_unknown_language_falls_back_to_base(self):
        assert i18n.bundle_js("zz") == i18n.bundle_js(BASE_LANG)
