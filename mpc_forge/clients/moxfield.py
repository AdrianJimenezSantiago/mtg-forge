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
        except Exception as e:
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


# Cabeceras de sección reconocidas. La sección "activa" en la state machine
# se aplica a todas las cartas siguientes hasta la próxima cabecera. Los
# nombres canónicos son los valores del dict; matchean varias variantes
# comunes usadas por Moxfield, MTGA, Deckstats, etc.
_SECTION_ALIASES: dict[str, str] = {
    "commander": "commander",
    "commanders": "commander",
    "companion": "companion",
    "companions": "companion",
    "mainboard": "mainboard",
    "main": "mainboard",
    "deck": "mainboard",
    "sideboard": "sideboard",
    "side": "sideboard",
    "maybeboard": "maybeboard",
    "maybe": "maybeboard",
    "tokens": "tokens",
}


def _detect_section_header(line: str) -> str | None:
    """Devuelve el nombre canónico de sección si `line` es una cabecera.

    Reconoce:
      - "//Commanders", "// Commanders" (formato Moxfield/MPCFill)
      - "Mainboard", "Mainboard (99)" (formato humano)
      - "SB:" en prefijo → sección sideboard (formato MTGO)
    Devuelve None si no es cabecera. Case-insensitive.
    """
    s = line.strip()
    if not s:
        return None
    # Formato "//Section" o "// Section"
    if s.startswith("//"):
        candidate = s.lstrip("/").strip().lower()
        # También aceptamos "//deck" seguido de "(99)"
        candidate = candidate.split("(", 1)[0].strip()
        return _SECTION_ALIASES.get(candidate)
    # Formato "Section" o "Section (N)" — solo si esa palabra suelta encaja.
    # Requerimos que la línea NO empiece por dígitos (una cantidad como "4
    # Sideboard" NO es cabecera, es una carta llamada Sideboard con qty 4).
    m = re.match(r"^([A-Za-z]+)(?:\s*\(\d+\))?\s*$", s)
    if m:
        return _SECTION_ALIASES.get(m.group(1).lower())
    return None


def parse_plain_decklist(text: str) -> list[dict[str, Any]]:
    """Parser para copy-paste tradicional. Formatos aceptados:
       "4 Lightning Bolt"
       "4x Lightning Bolt"
       "1 Sol Ring (C21) 263"
       "1 Sol Ring [C21] 263"
       "Lightning Bolt"  (asume 1)
       "SB: 2 Blood Moon" (prefijo MTGO — asigna sideboard)

    State machine (Extras · F1/T3): las cabeceras `//Commanders`,
    `//Mainboard`, `Sideboard`, `Maybeboard`, etc. establecen el rol que
    aplica a las cartas siguientes hasta el próximo cambio. Compatible con
    los ImportSites (Moxfield, Archidekt, MTGGoldfish) que emiten estos
    markers y con formatos humanos comunes (MTGA, MTGO, Deckstats).

    Sin cabeceras (import "puro" de texto): todo va a "mainboard" y
    ``create_deck_from_entries`` decide commander vía type_line — el
    comportamiento antiguo se preserva por retro-compat.

    Cada entrada incluye ``raw_line`` con el texto tal cual lo escribió el
    usuario, para que si la resolución falla podamos mostrárselo de vuelta
    exactamente igual (útil cuando hay typos o caracteres raros).
    """
    entries: list[dict[str, Any]] = []
    line_re = re.compile(
        r"^\s*(?P<qty>\d+)?\s*[xX]?\s+"
        r"(?P<name>[^\(\[\n]+?)"
        r"(?:\s+[\(\[](?P<set>[A-Za-z0-9]{2,6})[\)\]]"
        r"\s*(?P<num>\S+)?)?\s*$"
    )
    # Prefijo MTGO "SB:" → sideboard.
    sb_prefix_re = re.compile(r"^\s*SB:\s*", re.IGNORECASE)

    current_role = "mainboard"

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # 1) ¿Es una cabecera de sección? Actualiza state y salta.
        section = _detect_section_header(raw)
        if section is not None:
            current_role = section
            continue

        # 2) Prefijo MTGO "SB:" fuerza sideboard para ESTA línea concreta
        # sin cambiar la sección actual (útil en decklists mezcladas).
        role_for_this_line = current_role
        if sb_prefix_re.match(raw):
            role_for_this_line = "sideboard"
            raw_clean = sb_prefix_re.sub("", raw)
        else:
            raw_clean = raw

        m = line_re.match(raw_clean)
        if not m:
            # intento más simple: "Lightning Bolt"
            entries.append({
                "name": raw_clean.strip(), "quantity": 1, "set": None, "number": None,
                "role": role_for_this_line,
                "raw_line": raw,
            })
            continue
        entries.append({
            "name": m.group("name").strip(),
            "quantity": int(m.group("qty") or 1),
            "set": (m.group("set") or "").lower() or None,
            "number": m.group("num") or None,
            "role": role_for_this_line,
            "raw_line": raw,
        })
    return entries
