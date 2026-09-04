"""MTGGoldfish — endpoint público de descarga en texto plano.

Ruta: ``/deck/download/{deck_id}``. Devuelve texto tipo "N Cardname" con
secciones separadas por líneas en blanco.

Nombre: MTGGoldfish no expone una API pública de metadatos para mazos de
usuario. Solo los decks de arquetipo tienen el nombre en la ruta
(``/archetype/{name}``); para mazos con solo ``/deck/{id}`` no hay dato
fiable sin scraping de HTML.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError, _slug_to_title


class MTGGoldfishSite(ImportSite):
    key: ClassVar[str] = "mtggoldfish"
    name: ClassVar[str] = "MTGGoldfish"
    host_names: ClassVar[tuple[str, ...]] = ("www.mtggoldfish.com", "mtggoldfish.com")
    example_url: ClassVar[str] = "https://www.mtggoldfish.com/deck/1234567"
    base_url: ClassVar[str] = "www.mtggoldfish.com"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        path = urlparse(url).path or ""
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise InvalidURLError(url)
        deck_id = parts[-1].split("#", 1)[0]
        if not deck_id:
            raise InvalidURLError(url)
        resp = await cls.request(f"/deck/download/{deck_id}")
        return (resp.text or "").strip()

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Intenta extraer el nombre del mazo de la URL.

        Solo funciona para URLs de arquetipo (``/archetype/{name}``) donde
        el slug es el nombre del arquetipo. Para mazos de usuario
        (``/deck/{id}``) devuelve ``None`` — no hay dato sin scraping de HTML.
        """
        path = urlparse(url).path or ""
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "archetype":
            # /archetype/some-archetype-name → Some Archetype Name
            slug = parts[1].split("#", 1)[0]
            title = _slug_to_title(slug)
            return title if title else None
        return None
