"""TappedOut — export ``?fmt=txt`` sobre la propia URL del mazo.

TappedOut añade a cada URL de mazo la opción ``?fmt=txt`` que devuelve el
mazo entero en texto plano. Es la forma más simple de importar y no requiere
mapping de IDs (usamos el mismo path que el usuario pega).
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError


class TappedOutSite(ImportSite):
    key: ClassVar[str] = "tappedout"
    name: ClassVar[str] = "TappedOut"
    host_names: ClassVar[tuple[str, ...]] = ("tappedout.net", "www.tappedout.net")
    example_url: ClassVar[str] = "https://tappedout.net/mtg-decks/…"
    base_url: ClassVar[str] = "tappedout.net"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        path = urlparse(url).path or ""
        if not path or path == "/":
            raise InvalidURLError(url)
        resp = await cls.request(f"{path}?fmt=txt")
        text = resp.content.decode("utf-8", errors="replace")
        # TappedOut usa "Sideboard:" — normalizamos a nuestro marker estándar.
        text = text.replace("Sideboard:\r\n", "//Sideboard\n")
        text = text.replace("Sideboard:\n", "//Sideboard\n")
        return text.strip()
