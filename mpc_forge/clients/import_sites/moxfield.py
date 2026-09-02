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

    @classmethod
    def get_headers(cls) -> dict[str, str]:
        base = super().get_headers()
        base["Referer"] = "https://www.moxfield.com/"
        return base

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        deck_id = _extract_deck_id(url)
        if not deck_id:
            raise InvalidURLError(url)

        # v3 primero, v2 como fallback.
        payload = None
        last_err: Exception | None = None
        for base in (_MOX_API_V3, _MOX_API_V2):
            try:
                resp = await cls.request(f"{base}/decks/all/{deck_id}")
                payload = resp.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.debug("Moxfield %s falló: %s", base, e)

        if payload is None:
            raise ImportSiteError(
                f"No se pudo obtener el mazo {deck_id!r} de Moxfield: {last_err}"
            )

        return _payload_to_text(payload)


def _payload_to_text(payload: dict) -> str:
    """Convierte el JSON de Moxfield a texto plano estándar.

    Preserva la separación por secciones con markers ``//Commanders``,
    ``//Mainboard``, ``//Sideboard``, ``//Maybeboard`` para que
    ``parse_plain_decklist`` pueda inferir el rol al asignar cartas.
    (Actualmente parse_plain_decklist ignora estas cabeceras y todo va a
    mainboard; ver Fase 2 para uso de roles por sección.)
    """
    boards = payload.get("boards") or {}
    out: list[str] = []

    def _dump_board(board_key: str, label: str) -> None:
        cards = ((boards.get(board_key) or {}).get("cards") or {})
        if not cards:
            return
        out.append(f"//{label}")
        for _key, entry in cards.items():
            qty = entry.get("quantity", 1)
            card = entry.get("card") or {}
            name = card.get("name", "")
            if name:
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
