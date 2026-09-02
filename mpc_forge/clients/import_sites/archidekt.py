"""Archidekt — API JSON pública sin autenticación.

Endpoint público: ``https://archidekt.com/api/decks/{deck_id}/``.
Devuelve el mazo completo con cartas y categorías.
"""
from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, ImportSiteError, InvalidURLError

# Rutas típicas:
#   https://archidekt.com/decks/123456/my-deck
#   https://archidekt.com/decks/123456
_ARCHIDEKT_ID_RE = re.compile(r"^/decks/(\d+)")


class ArchidektSite(ImportSite):
    key: ClassVar[str] = "archidekt"
    name: ClassVar[str] = "Archidekt"
    host_names: ClassVar[tuple[str, ...]] = ("archidekt.com", "www.archidekt.com")
    example_url: ClassVar[str] = "https://archidekt.com/decks/1234567/deck-name"
    base_url: ClassVar[str] = "archidekt.com"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        path = urlparse(url).path or ""
        m = _ARCHIDEKT_ID_RE.search(path)
        if not m:
            raise InvalidURLError(url)
        deck_id = m.group(1)
        resp = await cls.request(f"/api/decks/{deck_id}/")
        payload = resp.json()
        cards = payload.get("cards") or []
        if not cards:
            raise ImportSiteError("El mazo de Archidekt no contiene cartas")

        # Archidekt devuelve una lista de cartas con categorías. Los "commanders"
        # y "sideboard" pueden estar marcados con `categories` (ej. ["Commander"]).
        # Segmentamos por sección para preservar el rol.
        commanders: list[tuple[int, str]] = []
        sideboard: list[tuple[int, str]] = []
        maybeboard: list[tuple[int, str]] = []
        mainboard: list[tuple[int, str]] = []

        for c in cards:
            qty = int(c.get("quantity", 1) or 1)
            oracle = c.get("card", {}).get("oracleCard") or {}
            name = oracle.get("name")
            if not name:
                continue
            categories = [cat.lower() for cat in (c.get("categories") or [])]
            if any("commander" in cat for cat in categories):
                commanders.append((qty, name))
            elif any(cat in ("sideboard", "side") for cat in categories):
                sideboard.append((qty, name))
            elif any(cat in ("maybeboard", "maybe") for cat in categories):
                maybeboard.append((qty, name))
            else:
                mainboard.append((qty, name))

        out: list[str] = []
        if commanders:
            out.append("//Commanders")
            out.extend(f"{q} {n}" for q, n in commanders)
            out.append("")
        if mainboard:
            out.append("//Mainboard")
            out.extend(f"{q} {n}" for q, n in mainboard)
            out.append("")
        if sideboard:
            out.append("//Sideboard")
            out.extend(f"{q} {n}" for q, n in sideboard)
            out.append("")
        if maybeboard:
            out.append("//Maybeboard")
            out.extend(f"{q} {n}" for q, n in maybeboard)
            out.append("")

        return "\n".join(out).strip()
