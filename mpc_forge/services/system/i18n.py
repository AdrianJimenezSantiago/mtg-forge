from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from mpc_forge.paths import resource_root

log = logging.getLogger(__name__)

LOCALES_DIR = resource_root() / "locales"


def _load_translations() -> dict[str, dict[str, str]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(LOCALES_DIR.glob("*.json"))
    }


_TRANSLATIONS: dict[str, dict[str, str]] = _load_translations()

SUPPORTED_LANGS: list[tuple[str, str]] = [
    ("es", "Español"),
    ("en", "English"),
]

LANG_FLAGS: dict[str, str] = {
    "es": "🇪🇸",
    "en": "🇬🇧",
}


BASE_LANG = "es"


class Translations:
    def __init__(self, lang: str) -> None:
        self._lang = lang
        self._data: dict[str, str] = _TRANSLATIONS.get(lang, _TRANSLATIONS[BASE_LANG])
        self._fallback: dict[str, str] = _TRANSLATIONS[BASE_LANG]

    def _lookup(self, key: str) -> str:
        value = self._data.get(key)
        if value is not None:
            return value
        value = self._fallback.get(key)
        if value is not None:
            log.warning(
                "Falta la traducción de %r en '%s'; se usa '%s'.",
                key,
                self._lang,
                BASE_LANG,
            )
            return value
        return f"[{key}]"

    def __getattr__(self, key: str) -> str:
        return self._lookup(key)

    def __getitem__(self, key: str) -> str:
        return self._lookup(key)

    def get(self, key: str, default: str = "") -> str:
        value = self._data.get(key) or self._fallback.get(key)
        return value if value is not None else default

    def as_dict(self) -> dict[str, str]:
        merged = dict(self._fallback)
        merged.update(self._data)
        return merged


def get_translations(lang: str) -> Translations:
    return Translations(lang if lang in _TRANSLATIONS else BASE_LANG)


def detect_lang(request_or_cookie: Any) -> str:
    if hasattr(request_or_cookie, "cookies"):
        cookie = request_or_cookie.cookies.get("lang", "es")
    elif isinstance(request_or_cookie, dict):
        cookie = request_or_cookie.get("lang", "es")
    else:
        cookie = "es"
    return cookie if cookie in _TRANSLATIONS else "es"


_BUNDLE_CACHE: dict[str, str] = {}
_VERSION_CACHE: str | None = None


def bundle_version() -> str:
    global _VERSION_CACHE
    if _VERSION_CACHE is None:
        payload = json.dumps(_TRANSLATIONS, sort_keys=True, ensure_ascii=False)
        _VERSION_CACHE = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return _VERSION_CACHE


def bundle_js(lang: str) -> str:
    lang = lang if lang in _TRANSLATIONS else BASE_LANG
    cached = _BUNDLE_CACHE.get(lang)
    if cached is not None:
        return cached

    data = get_translations(lang).as_dict()
    body = (
        f"window._LANG = {json.dumps(lang)};\n"
        f"window._T = {json.dumps(data, ensure_ascii=False)};\n"
        "window._t = (key) => window._T[key] ?? `[${key}]`;\n"
    )
    _BUNDLE_CACHE[lang] = body
    return body
