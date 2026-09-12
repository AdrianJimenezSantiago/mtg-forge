"""Scryfall — decks compartidos vía su API oficial.

Scryfall permite crear listas (decks) y exportarlas en texto plano vía
``https://api.scryfall.com/decks/{deck_id}/export/text``. La URL de
compartición pública sigue el formato ``https://scryfall.com/@user/decks/{id}``
o ``https://scryfall.com/decks/{id}``.

El nombre del deck se obtiene de la misma API en el endpoint de metadatos:
``https://api.scryfall.com/decks/{deck_id}`` → ``{"name": "…"}``.
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
            "Accept": "application/json;q=0.9,text/plain;q=0.8",
        }

    @classmethod
    def _extract_deck_id(cls, url: str) -> str | None:
        path = urlparse(url).path or ""
        m = _DECK_ID_RE.search(path)
        return m.group(1) if m else None

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        deck_id = cls._extract_deck_id(url)
        if not deck_id:
            raise InvalidURLError(url)
        resp = await cls.request(
            f"/decks/{deck_id}/export/text",
            netloc="api.scryfall.com",
        )
        text = resp.text or ""
        # Scryfall usa "// Sideboard" con espacio — normalizamos.
        return text.replace("// Sideboard", "//Sideboard").strip()

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Devuelve el nombre del deck desde la API de metadatos de Scryfall.

        Endpoint: ``api.scryfall.com/decks/{deck_id}`` → ``{"name": "…"}``.
        Llamada independiente del export de texto.
        """
        deck_id = cls._extract_deck_id(url)
        if not deck_id:
            return None
        try:
            resp = await cls.request(
                f"/decks/{deck_id}",
                netloc="api.scryfall.com",
            )
            payload = resp.json()
            name = (payload.get("name") or "").strip()
            return name if name else None
        except Exception:
            return None
