from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckActivity

log = logging.getLogger(__name__)


class DeckActivityKind:
    DECK_CREATED = "deck_created"
    DECK_RENAMED = "deck_renamed"
    DECK_LOCALIZED = "deck_localized"

    CARD_ADDED = "card_added"
    CARD_REMOVED = "card_removed"
    CARD_MOVED = "card_moved"
    CARD_QTY_CHANGED = "card_qty_changed"
    CARD_INCLUDE_TOGGLED = "card_include_toggled"
    CARD_ART_CHANGED = "card_art_changed"

    ROLE_CLEARED = "role_cleared"
    RELATED_ADDED = "related_added"

    XML_GENERATED = "xml_generated"
    PDF_GENERATED = "pdf_generated"
    IMAGES_EXPORTED = "images_exported"


_DEFAULT_SUMMARY: dict[str, str] = {
    DeckActivityKind.DECK_CREATED: "Mazo creado",
    DeckActivityKind.DECK_RENAMED: "Mazo renombrado",
    DeckActivityKind.DECK_LOCALIZED: "Idioma del mazo cambiado",
    DeckActivityKind.CARD_ADDED: "Carta añadida",
    DeckActivityKind.CARD_REMOVED: "Carta eliminada",
    DeckActivityKind.CARD_MOVED: "Carta movida de sección",
    DeckActivityKind.CARD_QTY_CHANGED: "Cantidad cambiada",
    DeckActivityKind.CARD_INCLUDE_TOGGLED: "Inclusión alternada",
    DeckActivityKind.CARD_ART_CHANGED: "Arte cambiado",
    DeckActivityKind.ROLE_CLEARED: "Sección vaciada",
    DeckActivityKind.RELATED_ADDED: "Cartas relacionadas añadidas",
    DeckActivityKind.XML_GENERATED: "XML generado",
    DeckActivityKind.PDF_GENERATED: "PDF generado",
    DeckActivityKind.IMAGES_EXPORTED: "Imágenes exportadas",
}


async def log_event(
    db: AsyncSession,
    deck_id: int | None,
    kind: str,
    *,
    card_name: str | None = None,
    card_scryfall_id: str | None = None,
    card_oracle_id: str | None = None,
    payload: dict[str, Any] | None = None,
    summary: str | None = None,
    deck_name: str | None = None,
) -> DeckActivity | None:
    try:
        if not deck_name and deck_id is not None:
            deck = await db.get(Deck, deck_id)
            deck_name = deck.name if deck else ""

        row = DeckActivity(
            deck_id=deck_id,
            deck_name_snapshot=deck_name or "",
            kind=kind,
            card_name=card_name,
            card_scryfall_id=card_scryfall_id,
            card_oracle_id=card_oracle_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            summary=summary or _DEFAULT_SUMMARY.get(kind, kind),
        )
        db.add(row)
        await db.flush()
        return row
    except Exception as e:
        log.warning("Fallo registrando actividad %r del mazo %s: %s", kind, deck_id, e)
        return None


async def list_for_deck(
    db: AsyncSession,
    deck_id: int,
    kinds: list[str] | None = None,
    limit: int = 500,
) -> list[DeckActivity]:
    stmt = (
        select(DeckActivity)
        .where(DeckActivity.deck_id == deck_id)
        .order_by(DeckActivity.created_at.desc())
        .limit(limit)
    )
    if kinds:
        stmt = stmt.where(DeckActivity.kind.in_(kinds))
    return list((await db.scalars(stmt)).all())
