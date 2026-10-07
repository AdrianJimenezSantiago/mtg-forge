from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, cast

import httpx

from mpc_forge import config as cfg
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_MOXFIELD_API_BASE = "https://api2.moxfield.com/v3"
_MOXFIELD_API_LEGACY = "https://api2.moxfield.com/v2"

_DECK_ID_RE = re.compile(r"moxfield\.com/decks/([A-Za-z0-9_-]+)")


class MoxfieldError(RuntimeError):
    pass


def extract_deck_id(url_or_id: str) -> str:
    m = _DECK_ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    return url_or_id.strip()


class MoxfieldClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": cfg.MOXFIELD_USER_AGENT,
                "Accept": "application/json",
                "Referer": "https://www.moxfield.com/",
            },
            timeout=30.0,
            follow_redirects=True,
            verify=not ssl_insecure(),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_deck(self, url_or_id: str) -> dict[str, Any]:
        deck_id = extract_deck_id(url_or_id)
        try:
            resp = await self._client.get(f"{_MOXFIELD_API_BASE}/decks/all/{deck_id}")
            if resp.status_code == 200:
                return cast(dict[str, Any], resp.json())
        except httpx.HTTPError as e:
            log.debug("Moxfield v3 falló: %s", e)
        try:
            resp = await self._client.get(f"{_MOXFIELD_API_LEGACY}/decks/all/{deck_id}")
            if resp.status_code == 200:
                return cast(dict[str, Any], resp.json())
        except httpx.HTTPError as e:
            log.debug("Moxfield v2 falló: %s", e)
        return await asyncio.to_thread(_cloudscraper_fetch, deck_id)


def _cloudscraper_fetch(deck_id: str) -> dict[str, Any]:
    import cloudscraper

    scraper = cloudscraper.create_scraper()
    scraper.headers.update(
        {
            "User-Agent": cfg.MOXFIELD_USER_AGENT,
            "Referer": "https://www.moxfield.com/",
        }
    )
    for base in (_MOXFIELD_API_BASE, _MOXFIELD_API_LEGACY):
        try:
            r = scraper.get(f"{base}/decks/all/{deck_id}", timeout=30)
            if r.status_code == 200:
                return cast(dict[str, Any], r.json())
        except Exception as e:
            log.debug("cloudscraper %s falló: %s", base, e)
    raise MoxfieldError(
        f"No se pudo obtener el mazo {deck_id!r}. "
        "Es privado, no existe, o Moxfield está bloqueando; "
        "pega la lista de cartas manualmente en su lugar."
    )


def normalize_deck(payload: dict[str, Any]) -> dict[str, Any]:
    deck_id = payload.get("publicId") or payload.get("id") or ""
    name = payload.get("name", "Imported deck")
    fmt = (payload.get("format") or "commander").lower()
    boards = payload.get("boards") or {}
    result_cards: list[dict[str, Any]] = []
    commander_info: dict[str, str] | None = None

    board_roles = {
        "mainboard": "mainboard",
        "commanders": "commander",
        "companions": "companion",
        "sideboard": "sideboard",
        "maybeboard": "maybeboard",
        "tokens": "tokens",
    }

    for board_key, role in board_roles.items():
        board = boards.get(board_key) or {}
        cards_dict = board.get("cards") or {}
        for entry in cards_dict.values():
            qty = entry.get("quantity", 1)
            card = entry.get("card") or {}
            info = {
                "name": card.get("name", ""),
                "quantity": qty,
                "scryfall_id": card.get("scryfall_id") or card.get("id") or "",
                "set": (card.get("set") or "").lower(),
                "number": card.get("cn") or card.get("collector_number") or "",
                "oracle_id": card.get("oracle_id") or "",
                "role": role,
            }
            result_cards.append(info)
            if role == "commander" and commander_info is None:
                commander_info = {
                    "name": info["name"],
                    "scryfall_id": info["scryfall_id"],
                }

    return {
        "moxfield_id": deck_id,
        "name": name,
        "format": fmt,
        "source_url": f"https://www.moxfield.com/decks/{deck_id}" if deck_id else None,
        "commander": commander_info,
        "cards": result_cards,
    }
