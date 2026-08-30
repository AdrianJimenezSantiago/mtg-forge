"""Log de actividad por mazo (timeline).

Escribe filas en ``DeckActivity`` cada vez que ocurre algo relevante sobre un
mazo. La API pública es una única función ``log()`` a la que llaman los routes
justo antes de hacer commit (así la actividad es transaccional con la
operación en sí).

Diseño:

* ``kind`` es un string libre; los tipos que reconoce el frontend viven en el
  módulo ``DeckActivityKind`` de aquí abajo (mera documentación centralizada).
* ``payload`` es un dict cualquiera; lo serializamos como JSON. El frontend
  formatea cada kind con su propia función de render.
* ``summary`` se auto-genera en Python si no se pasa — es solo un fallback de
  legibilidad; el frontend tiene renderers mejores para casi todo.
* El caller es responsable del ``commit()`` — nosotros solo hacemos ``flush``.
  Esto asegura que la actividad se registra en la misma transacción que la
  operación (si el commit falla, no queda un "fantasma" en el timeline).

Errores de logging NUNCA rompen el flujo. Si algo falla al escribir el
timeline, lo tragamos con un log de warning — la operación de negocio es más
importante que su registro.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckActivity

log = logging.getLogger(__name__)


class DeckActivityKind:
    """Constantes de tipos de eventos. Centralizadas para poder buscarlas.

    El frontend (history.html → KIND_META) tiene un renderer para cada uno.
    Añadir aquí + añadir renderer allí = feature completa.
    """
    DECK_CREATED = "deck_created"       # payload: {source: 'moxfield'|'text'|'manual', card_count, unresolved_count}
    DECK_RENAMED = "deck_renamed"        # payload: {old_name, new_name}
    DECK_LOCALIZED = "deck_localized"    # payload: {lang, localized, unchanged, unavailable, skipped_custom}

    CARD_ADDED = "card_added"            # payload: {quantity, role}
    CARD_REMOVED = "card_removed"        # payload: {quantity, role}
    CARD_MOVED = "card_moved"            # payload: {from_role, to_role}
    CARD_QTY_CHANGED = "card_qty_changed"        # payload: {old_qty, new_qty}
    CARD_INCLUDE_TOGGLED = "card_include_toggled"  # payload: {new_include: bool}
    CARD_ART_CHANGED = "card_art_changed"        # payload: {old_scryfall_id, new_scryfall_id, old_set, new_set, remember_globally, custom_art_id, face}

    ROLE_CLEARED = "role_cleared"        # payload: {role, count}
    RELATED_ADDED = "related_added"      # payload: {count, kinds:[token,meld_result…]}

    XML_GENERATED = "xml_generated"      # payload: {cardstock, foil, total_cards, tier_size, xml_path}
    PDF_GENERATED = "pdf_generated"      # payload: {page_size, cut_marks, include_backs, total_slots, total_pages, pdf_path}
    IMAGES_EXPORTED = "images_exported"  # payload: {total_files, total_unique_cards, total_dfc_backs, zip_path, zip_filename}


# Etiquetas legibles usadas si el caller no pasa summary a mano.
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
    """Registra un evento en el timeline del mazo.

    NO hace commit — asume que el caller commiteará poco después. Solo hace
    ``flush()`` para que el id se genere.

    Si ``deck_name`` no se pasa y ``deck_id`` está, lo lee de BD. Si el mazo
    no existe (evento huérfano), guarda el nombre snapshot vacío.

    Errores se tragan con warning — nunca romper el flujo por un fallo de log.
    """
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
    except Exception as e:  # noqa: BLE001
        log.warning("Fallo registrando actividad %r del mazo %s: %s", kind, deck_id, e)
        return None


async def list_for_deck(
    db: AsyncSession,
    deck_id: int,
    kinds: list[str] | None = None,
    limit: int = 500,
) -> list[DeckActivity]:
    """Devuelve las últimas ``limit`` entradas para un mazo, más recientes primero.

    Filtro opcional por tipos. Los ``limit`` es un tope duro para evitar payloads
    gigantes en mazos muy trabajados.
    """
    stmt = (
        select(DeckActivity)
        .where(DeckActivity.deck_id == deck_id)
        .order_by(DeckActivity.created_at.desc())
        .limit(limit)
    )
    if kinds:
        stmt = stmt.where(DeckActivity.kind.in_(kinds))
    return list((await db.scalars(stmt)).all())


async def count_for_deck(db: AsyncSession, deck_id: int) -> int:
    """Cuenta rápida de eventos para pintar el badge en la card del mazo."""
    from sqlalchemy import func
    result = await db.execute(
        select(func.count(DeckActivity.id)).where(DeckActivity.deck_id == deck_id)
    )
    return int(result.scalar_one() or 0)


async def last_activity_for_deck(
    db: AsyncSession, deck_id: int
) -> DeckActivity | None:
    """Último evento — usado para "última actividad hace X" en la card."""
    stmt = (
        select(DeckActivity)
        .where(DeckActivity.deck_id == deck_id)
        .order_by(DeckActivity.created_at.desc())
        .limit(1)
    )
    return (await db.scalars(stmt)).first()
