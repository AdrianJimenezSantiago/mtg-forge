"""MTGGoldfish — endpoint público de descarga en texto plano.

Ruta: ``/deck/download/{deck_id}``. Devuelve texto tipo "N Cardname" con
secciones separadas por líneas en blanco. Es el mismo path que usa MPC Autofill.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError


class MTGGoldfishSite(ImportSite):
    key: ClassVar[str] = "mtggoldfish"
    name: ClassVar[str] = "MTGGoldfish"
    host_names: ClassVar[tuple[str, ...]] = ("www.mtggoldfish.com", "mtggoldfish.com")
    example_url: ClassVar[str] = "https://www.mtggoldfish.com/deck/1234567"
    base_url: ClassVar[str] = "www.mtggoldfish.com"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        # Ruta típica: /deck/1234567 o /archetype/{name}/decks/1234567 (más raro)
        path = urlparse(url).path or ""
        # Aísla el ID: última parte numérica antes de un posible fragmento.
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise InvalidURLError(url)
        deck_id = parts[-1].split("#", 1)[0]
        if not deck_id:
            raise InvalidURLError(url)
        resp = await cls.request(f"/deck/download/{deck_id}")
        return (resp.text or "").strip()
