"""Cliente para Moxfield (API pública no oficial).

Moxfield no tiene API pública documentada; usamos los mismos endpoints que su web.
En Windows suele funcionar con httpx + un User-Agent identificable.
Si Cloudflare bloquea, hacemos fallback a cloudscraper (síncrono) en un thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

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
    """Acepta URL completa o solo el ID y devuelve el ID."""
    m = _DECK_ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    # Si no matchea, asumimos que es un ID directo.
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
        """Devuelve el JSON del mazo, probando v3 → v2 → cloudscraper."""
        deck_id = extract_deck_id(url_or_id)
        # 1) v3
        try:
            resp = await self._client.get(f"{_MOXFIELD_API_BASE}/decks/all/{deck_id}")
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError as e:
            log.debug("Moxfield v3 falló: %s", e)
        # 2) v2 (fallback)
        try:
            resp = await self._client.get(f"{_MOXFIELD_API_LEGACY}/decks/all/{deck_id}")
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError as e:
            log.debug("Moxfield v2 falló: %s", e)
        # 3) cloudscraper (bloqueante, en thread) como último recurso
        return await asyncio.to_thread(_cloudscraper_fetch, deck_id)


def _cloudscraper_fetch(deck_id: str) -> dict[str, Any]:
    """Fallback síncrono usando cloudscraper para pasar el JS challenge."""
    import cloudscraper  # import perezoso: solo si hace falta

    scraper = cloudscraper.create_scraper()
    scraper.headers.update({
        "User-Agent": cfg.MOXFIELD_USER_AGENT,
        "Referer": "https://www.moxfield.com/",
    })
    for base in (_MOXFIELD_API_BASE, _MOXFIELD_API_LEGACY):
        try:
            r = scraper.get(f"{base}/decks/all/{deck_id}", timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception as e:  # noqa: BLE001
            log.debug("cloudscraper %s falló: %s", base, e)
    raise MoxfieldError(
        f"No se pudo obtener el mazo {deck_id!r}. "
        "Es privado, no existe, o Moxfield está bloqueando; "
        "pega la lista de cartas manualmente en su lugar."
    )


# --- Normalización -------------------------------------------------------

def normalize_deck(payload: dict[str, Any]) -> dict[str, Any]:
    """Convierte la respuesta de Moxfield a una estructura interna estable.

    Devuelve:
        {
          "name": str,
          "format": str,
          "source_url": str,
          "moxfield_id": str,
          "commander": {name, scryfall_id} | None,
          "cards": [{name, quantity, scryfall_id, set, number, role}, ...],
        }
    """
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
        for _card_key, entry in cards_dict.items():
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


def parse_plain_decklist(text: str) -> list[dict[str, Any]]:
    """Parser para copy-paste tradicional. Formatos aceptados:
       "4 Lightning Bolt"
       "4x Lightning Bolt"
       "1 Sol Ring (C21) 263"
       "1 Sol Ring [C21] 263"
       "Lightning Bolt"  (asume 1)
    """
    entries: list[dict[str, Any]] = []
    line_re = re.compile(
        r"^\s*(?P<qty>\d+)?\s*[xX]?\s+"
        r"(?P<name>[^\(\[\n]+?)"
        r"(?:\s+[\(\[](?P<set>[A-Za-z0-9]{2,6})[\)\]]"
        r"\s*(?P<num>\S+)?)?\s*$"
    )
    for raw in text.splitlines():
        if not raw.strip() or raw.strip().startswith("//") or raw.strip().startswith("#"):
            continue
        # Cabeceras tipo "Mainboard (99)" u otras — ignoramos las que no tienen nombre.
        if re.match(r"^(mainboard|commander|sideboard|maybeboard|tokens)\b", raw, re.I):
            continue
        m = line_re.match(raw)
        if not m:
            # intento más simple: "Lightning Bolt"
            entries.append({"name": raw.strip(), "quantity": 1, "set": None, "number": None})
            continue
        entries.append({
            "name": m.group("name").strip(),
            "quantity": int(m.group("qty") or 1),
            "set": (m.group("set") or "").lower() or None,
            "number": m.group("num") or None,
        })
    return entries
