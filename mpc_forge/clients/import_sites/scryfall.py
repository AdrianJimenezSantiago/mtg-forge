"""Scryfall — decks compartidos vía su API oficial.

Scryfall permite crear listas (decks) y exportarlas en texto plano vía
``https://api.scryfall.com/decks/{deck_id}/export/text``. La URL de
compartición pública sigue el formato ``https://scryfall.com/@user/decks/{id}``
o ``https://scryfall.com/decks/{id}``.
"""
from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlparse

from mpc_forge.config import SCRYFALL_USER_AGENT

from .base import ImportSite, InvalidURLError

# Cualquier UUID/slug tras "/decks/"
_DECK_ID_RE = re.compile(r"/decks/([A-Za-z0-9\-]+)")


class ScryfallSite(ImportSite):
    key: ClassVar[str] = "scryfall"
    name: ClassVar[str] = "Scryfall"
    host_names: ClassVar[tuple[str, ...]] = ("scryfall.com", "www.scryfall.com")
    example_url: ClassVar[str] = "https://scryfall.com/@user/decks/…"

    @classmethod
    def get_headers(cls) -> dict[str, str]:
        return {
            "User-Agent": SCRYFALL_USER_AGENT,
            "Accept": "text/plain",
        }

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        path = urlparse(url).path or ""
        m = _DECK_ID_RE.search(path)
        if not m:
            raise InvalidURLError(url)
        deck_id = m.group(1)
        # El endpoint text export vive en api.scryfall.com, no en scryfall.com.
        resp = await cls.request(
            f"/decks/{deck_id}/export/text",
            netloc="api.scryfall.com",
        )
        text = resp.text or ""
        # Scryfall usa "// Sideboard" con espacio — el parser también lo tolera,
        # pero normalizamos para consistencia.
        return text.replace("// Sideboard", "//Sideboard").strip()
