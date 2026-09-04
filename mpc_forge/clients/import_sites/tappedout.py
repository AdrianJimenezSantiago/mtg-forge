"""TappedOut — export ``?fmt=txt`` sobre la propia URL del mazo.

TappedOut añade a cada URL de mazo la opción ``?fmt=txt`` que devuelve el
mazo entero en texto plano.

El nombre del mazo está en el slug de la URL:
  ``/mtg-decks/my-commander-deck/`` → ``My Commander Deck``
Sin fetch adicional.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError, _slug_to_title


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
        text = text.replace("Sideboard:\r\n", "//Sideboard\n")
        text = text.replace("Sideboard:\n", "//Sideboard\n")
        return text.strip()

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Extrae el nombre del mazo del slug de la URL.

        ``/mtg-decks/my-commander-deck/`` → ``My Commander Deck``.
        Sin fetch adicional.
        """
        path = urlparse(url).path or ""
        # Ruta: /mtg-decks/{slug}/ — tomamos el último segmento no vacío.
        parts = [p for p in path.split("/") if p and p != "mtg-decks"]
        if not parts:
            return None
        slug = parts[-1]
        title = _slug_to_title(slug)
        return title if title else None
