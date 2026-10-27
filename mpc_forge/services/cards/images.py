from __future__ import annotations

import re
from typing import Any

WEBP_FOR = {
    "small": "thumb",
    "normal": "grid",
    "large": "display",
    "border_crop": "crop",
    "art_crop": "art",
}

_JPEG_URL = re.compile(
    r"^(https://cards\.scryfall\.io/)(small|normal|large|border_crop|art_crop)/(.+)\.jpg(\?.*)?$"
)


def webp_url(url: str | None) -> str | None:
    if not url:
        return url
    m = _JPEG_URL.match(url)
    if not m:
        return url
    base, version, path, query = m.groups()
    return f"{base}{WEBP_FOR[version]}/{path}.webp{query or ''}"


_VERSION_DIR = re.compile(r"^(https://cards\.scryfall\.io/)[a-z_]+/")


def variant(url: str | None, version: str) -> str | None:
    """La misma imagen WebP en otro tamaño (grid → thumb, display…)."""
    url = webp_url(url)
    if not url or not _VERSION_DIR.match(url):
        return None
    return _VERSION_DIR.sub(rf"\g<1>{version}/", url, count=1)


def pick(uris: dict[str, Any], webp: str, *legacy: str) -> str | None:
    """La versión WebP si la hay; si no, la primera JPEG antigua convertida."""
    if uris.get(webp):
        return str(uris[webp])
    for key in legacy:
        if uris.get(key):
            return webp_url(str(uris[key]))
    return None


def grid(uris: dict[str, Any]) -> str | None:
    return pick(uris, "grid", "jpeg", "normal")


def display(uris: dict[str, Any]) -> str | None:
    return pick(uris, "display", "large")


def thumb(uris: dict[str, Any]) -> str | None:
    return pick(uris, "thumb", "small")
