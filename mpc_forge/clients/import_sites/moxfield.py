"""Moxfield: API v2/v3 → texto plano.

Devolvemos el texto plano en el formato universal para que la pipeline común
lo procese. La lógica avanzada (detección de commander/companion,
scryfall_ids exactos por impresión) sigue viva en ``MoxfieldClient``, que se
usa para el import "rico" existente. Este `ImportSite` es el path unificado.
"""
from __future__ import annotations

import logging
import re
from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, ImportSiteError, InvalidURLError

log = logging.getLogger(__name__)

_MOX_API_V3 = "https://api2.moxfield.com/v3"
_MOX_API_V2 = "https://api2.moxfield.com/v2"

_DECK_ID_RE = re.compile(r"^/decks/([A-Za-z0-9_-]+)")


def _extract_deck_id(url: str) -> str | None:
    m = _DECK_ID_RE.search(urlparse(url).path or "")
    return m.group(1) if m else None


class MoxfieldSite(ImportSite):
    key: ClassVar[str] = "moxfield"
    name: ClassVar[str] = "Moxfield"
    host_names: ClassVar[tuple[str, ...]] = ("www.moxfield.com", "moxfield.com")
    example_url: ClassVar[str] = "https://www.moxfield.com/decks/…"

    # Cache temporal del último payload descargado. Se rellena en
    # retrieve_card_list y se consulta en retrieve_deck_name para no
    # hacer un segundo fetch. Usamos un dict con clave = deck_id para
    # evitar colisiones en llamadas concurrentes improbables.
    _payload_cache: ClassVar[dict[str, dict]] = {}

    @classmethod
    def get_headers(cls) -> dict[str, str]:
        base = super().get_headers()
        base["Referer"] = "https://www.moxfield.com/"
        return base

    @classmethod
    async def _fetch_payload(cls, url: str) -> tuple[str, dict]:
        """Descarga y devuelve (deck_id, payload). Guarda en _payload_cache."""
        deck_id = _extract_deck_id(url)
        if not deck_id:
            raise InvalidURLError(url)

        if deck_id in cls._payload_cache:
            return deck_id, cls._payload_cache[deck_id]

        payload = None
        last_err: Exception | None = None
        for base in (_MOX_API_V3, _MOX_API_V2):
            try:
                resp = await cls.request(f"{base}/decks/all/{deck_id}")
                payload = resp.json()
                break
            except Exception as e:
                last_err = e
                log.debug("Moxfield %s falló: %s", base, e)

        if payload is None:
            raise ImportSiteError(
                f"No se pudo obtener el mazo {deck_id!r} de Moxfield: {last_err}"
            )

        cls._payload_cache[deck_id] = payload
        return deck_id, payload

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        _deck_id, payload = await cls._fetch_payload(url)
        return _payload_to_text(payload)

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Devuelve el nombre del mazo tal como está en Moxfield.

        Reutiliza el payload ya descargado en ``retrieve_card_list`` si
        estaba cacheado — sin segundo fetch. Si ``retrieve_card_list`` aún
        no se llamó (uso directo), hace el fetch.

        El campo ``name`` del JSON de Moxfield es el título que el usuario
        le puso al mazo, ej. "Ultimate Cloud Deck".
        """
        deck_id, payload = await cls._fetch_payload(url)
        # Limpiar cache tras leer el nombre para no acumular memoria
        cls._payload_cache.pop(deck_id, None)
        deck_name = (payload.get("name") or "").strip()
        return deck_name if deck_name else None


def _payload_to_text(payload: dict) -> str:
    """Convierte el JSON de Moxfield a texto plano estándar.

    Preserva la separación por secciones con markers ``//Commanders``,
    ``//Mainboard``, ``//Sideboard``, ``//Maybeboard`` para que
    ``parse_plain_decklist`` pueda inferir el rol al asignar cartas.

    Bug-fix DFC (Extras): Moxfield devuelve ``card.set`` y ``card.cn`` en el
    JSON. Si están presentes los incluimos en el formato estándar
    ``N Nombre (SET) CN`` para que ``resolve_cards`` use el lookup por
    set+collector_number — mucho más fiable que búsqueda por nombre,
    especialmente para DFCs con ``//`` en el nombre (ej. cartas FF UB,
    Alchemy transformers, etc.).

    Si Moxfield no proporciona set/cn (caso raro), caemos al nombre solo.
    """
    boards = payload.get("boards") or {}
    out: list[str] = []

    def _dump_board(board_key: str, label: str) -> None:
        cards = ((boards.get(board_key) or {}).get("cards") or {})
        if not cards:
            return
        out.append(f"//{label}")
        for entry in cards.values():
            qty = entry.get("quantity", 1)
            card = entry.get("card") or {}
            name = card.get("name", "")
            if not name:
                continue

            set_code = (card.get("set") or card.get("setCode") or "").strip().lower()
            cn = (card.get("cn") or card.get("collectorNumber") or "").strip()

            if set_code and cn:
                # Formato estándar con set+CN: resolución exacta, sin fuzz.
                # Especialmente crítico para DFCs ("Front // Back") donde el
                # nombre puede tener espacios y // que confunden al fuzzy matcher.
                out.append(f"{qty} {name} ({set_code}) {cn}")
            else:
                out.append(f"{qty} {name}")
        out.append("")

    _dump_board("commanders", "Commanders")
    _dump_board("companions", "Companions")
    _dump_board("mainboard", "Mainboard")
    _dump_board("sideboard", "Sideboard")
    _dump_board("maybeboard", "Maybeboard")

    if not out:
        raise ImportSiteError("El mazo de Moxfield no tiene cartas visibles")

    return "\n".join(out).strip()
