from __future__ import annotations

import re
from typing import Any

# Scryfall retira el 1-1-2027 los JPEG small/large/border_crop/art_crop y pide
# usar WebP en el navegador; normal sigue existiendo (renombrado a «jpeg»).
# Las URLs nuevas son las mismas con otra carpeta y extensión.
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
