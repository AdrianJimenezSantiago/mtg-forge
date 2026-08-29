"""Deshacer eventos del timeline.

Reutiliza los snapshots que ``DeckActivity`` guarda antes/después de cada
operación para poder aplicarlas al revés. No es un undo genérico basado en
diff — es un handler explícito por cada ``kind`` que sabemos revertir.

Lo que **se puede** deshacer (los payloads llevan la info suficiente):

* ``card_moved``           → mover de nuevo al ``from_role``
* ``card_qty_changed``     → restaurar ``old_qty``
* ``card_include_toggled`` → invertir ``include``
* ``card_art_changed``     → restaurar ``old_scryfall_id`` (solo cambios de
                             printing oficial; los cambios a custom art no
                             son reversibles porque el custom podría haberse
                             borrado del disco entre medias)
* ``deck_renamed``         → restaurar ``old_name``
* ``card_added``           → eliminar la carta añadida (si sigue existiendo)
* ``card_removed``         → re-crear la carta con quantity/role del snapshot

Lo que **no** se puede deshacer:

* ``deck_created``          — deshacer sería borrar el mazo entero; el usuario
                              tiene el botón de borrar mazo aparte, no lo
                              convertimos en undo silencioso
* ``deck_localized``        — múltiples cartas afectadas, restaurar sería
                              rehacer N llamadas a Scryfall; muy caro
* ``role_cleared``          — borrar el sideboard entero es tan grande que no
                              queremos ofrecer "deshacer" como si nada
* ``related_added``         — múltiples cartas, mismo problema
* ``xml_generated`` / ``pdf_generated`` — no hay estado que revertir
* ``card_art_changed`` a/desde custom — el CustomArt referenciado podría no
                              existir ya

Cada undo emite un nuevo evento (``kind`` con prefijo ``undo_``, o el kind
opuesto) para que el timeline refleje la operación. NO se marca el evento
original como "deshecho" para no falsear el histórico.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckActivity, DeckCard, PrintingCache
from mpc_forge.services import deck_activity
from mpc_forge.services.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


# Kinds que sabemos revertir. Usado por el frontend para saber qué eventos
# muestran el botón "Deshacer".
UNDOABLE_KINDS: set[str] = {
    K.CARD_MOVED,
    K.CARD_QTY_CHANGED,
    K.CARD_INCLUDE_TOGGLED,
    K.CARD_ART_CHANGED,
    K.DECK_RENAMED,
    K.CARD_ADDED,
    K.CARD_REMOVED,
}


class UndoNotSupported(Exception):
    """El tipo de evento no admite undo, o el estado ha cambiado tanto que
    la operación inversa ya no es aplicable con seguridad."""


async def can_undo(event: DeckActivity) -> tuple[bool, str]:
    """Comprueba a priori si un evento se puede deshacer.

    Devuelve ``(True, "")`` o ``(False, "razón")``. La comprobación es
    sintáctica (solo mira kind + payload); la de estado en BD la hace
    ``undo_event()`` en el momento de ejecutar.
    """
    if event.kind not in UNDOABLE_KINDS:
        return False, "Tipo de evento no reversible"

    try:
        payload = json.loads(event.payload_json or "{}")
    except (ValueError, TypeError):
        return False, "Payload corrupto"

    if event.kind == K.CARD_ART_CHANGED:
        # Cambios a custom art no se deshacen: el archivo podría no existir.
        if payload.get("kind") == "custom":
            return False, "Cambios a arte custom no se pueden deshacer automáticamente"
        if not payload.get("old_scryfall_id"):
            return False, "Falta el arte anterior en el snapshot"

    return True, ""


async def undo_event(db: AsyncSession, event: DeckActivity) -> dict[str, Any]:
    """Aplica la operación inversa del evento y registra un nuevo evento en el
    timeline reflejando el undo. Hace commit al final.

    Devuelve un dict con detalles legibles del undo aplicado (para el toast).

    Lanza ``UndoNotSupported`` si el kind no es reversible, si el payload no
    tiene la info necesaria, o si el estado actual no permite aplicar la
    operación inversa (p. ej. la carta ya se borró después).
    """
    ok, reason = await can_undo(event)
    if not ok:
        raise UndoNotSupported(reason)

    payload = json.loads(event.payload_json or "{}")
    deck_id = event.deck_id
    if deck_id is None:
        raise UndoNotSupported("Evento sin mazo asociado")

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise UndoNotSupported("El mazo ya no existe")

    kind = event.kind
    handler = _HANDLERS.get(kind)
    if handler is None:
        # Guardaespaldas — no debería pasar porque UNDOABLE_KINDS lo cubre
        raise UndoNotSupported(f"Sin handler para {kind}")

    summary = await handler(db, event, payload, deck)

    # Nuevo evento en el timeline reflejando el undo. Reutilizamos el kind
    # opuesto cuando existe, para que la vista lo pinte con su icono normal;
    # si no, ponemos un kind genérico "undone".
    inverse_kind = _INVERSE_KIND.get(kind, "undone")
    await deck_activity.log_event(
        db, deck_id, inverse_kind,
        card_name=event.card_name,
        card_scryfall_id=event.card_scryfall_id,
        card_oracle_id=event.card_oracle_id,
        payload={
            **summary.get("payload_extras", {}),
            "undone_event_id": event.id,
            "undone_kind": kind,
        },
        summary=summary["summary"],
        deck_name=deck.name,
    )
    await db.commit()
    return summary


# --- Handlers por kind ----------------------------------------------------

async def _undo_card_moved(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    from_role = payload.get("from_role")
    to_role = payload.get("to_role")
    if not from_role or not to_role:
        raise UndoNotSupported("Snapshot incompleto de roles")

    # Buscamos la carta por scryfall_id + rol actual. Si el usuario la ha
    # movido otra vez desde entonces, ya no está en `to_role` — no podemos
    # revertir con seguridad.
    dc = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == event.card_scryfall_id,
            DeckCard.role == to_role,
        )
    )).first()
    if not dc:
        raise UndoNotSupported(
            f"La carta ya no está en '{to_role}' — se ha movido de nuevo desde entonces"
        )
    dc.role = from_role
    return {
        "summary": f"Deshecho: {event.card_name} vuelve a {from_role}",
        "payload_extras": {"from_role": to_role, "to_role": from_role},
    }


async def _undo_card_qty_changed(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    old_qty = payload.get("old_qty")
    new_qty = payload.get("new_qty")
    if old_qty is None or new_qty is None:
        raise UndoNotSupported("Snapshot incompleto de cantidad")

    dc = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == event.card_scryfall_id,
        )
    )).first()
    if not dc:
        raise UndoNotSupported("La carta ya no existe en el mazo")
    if dc.quantity != new_qty:
        raise UndoNotSupported(
            f"La cantidad ha cambiado desde entonces ({new_qty} → {dc.quantity})"
        )
    dc.quantity = old_qty
    return {
        "summary": f"Deshecho: {event.card_name} vuelve a {old_qty}×",
        "payload_extras": {"old_qty": new_qty, "new_qty": old_qty},
    }


async def _undo_card_include_toggled(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    prev_include = not payload.get("new_include", True)
    dc = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == event.card_scryfall_id,
        )
    )).first()
    if not dc:
        raise UndoNotSupported("La carta ya no existe")
    dc.include = prev_include
    label = "reactivada" if prev_include else "excluida"
    return {
        "summary": f"Deshecho: {event.card_name} {label}",
        "payload_extras": {"new_include": prev_include},
    }


async def _undo_card_art_changed(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    old_sfid = payload.get("old_scryfall_id")
    new_sfid = payload.get("new_scryfall_id")
    if not old_sfid or not new_sfid:
        raise UndoNotSupported("Snapshot incompleto del arte")

    # La carta debería estar aún con new_scryfall_id. Si el usuario cambió el
    # arte de nuevo, ya no está — no revertimos.
    dc = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == new_sfid,
        )
    )).first()
    if not dc:
        raise UndoNotSupported("El arte ha cambiado desde entonces — ya no coincide")

    # El printing anterior debería seguir cacheado (nunca borramos rows de
    # PrintingCache). Si por lo que sea no está, damos error legible.
    old_printing = await db.get(PrintingCache, old_sfid)
    if not old_printing:
        raise UndoNotSupported("La impresión anterior ya no está en caché")

    dc.scryfall_id = old_sfid
    old_set = payload.get("old_set", "?")
    old_num = payload.get("old_number", "?")
    return {
        "summary": f"Deshecho: arte de {event.card_name} vuelve a {str(old_set).upper()} {old_num}",
        "payload_extras": {
            "kind": "official",
            "old_scryfall_id": new_sfid,
            "new_scryfall_id": old_sfid,
            "old_set": payload.get("new_set"),
            "old_number": payload.get("new_number"),
            "new_set": payload.get("old_set"),
            "new_number": payload.get("old_number"),
        },
    }


async def _undo_deck_renamed(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    old_name = payload.get("old_name")
    new_name = payload.get("new_name")
    if not old_name:
        raise UndoNotSupported("Sin nombre anterior en el snapshot")
    if deck.name != new_name:
        raise UndoNotSupported(
            f"El mazo ya no se llama '{new_name}' — se ha renombrado de nuevo"
        )
    deck.name = old_name
    return {
        "summary": f"Deshecho: mazo vuelve a llamarse '{old_name}'",
        "payload_extras": {"old_name": new_name, "new_name": old_name},
    }


async def _undo_card_added(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    # Deshacer un add = eliminar la carta. Si se hizo stack (quantity +=),
    # deberíamos revertir solo la cantidad añadida, no borrar la carta entera.
    stacked = payload.get("stacked", False)
    added_qty = payload.get("quantity", 1)
    role = payload.get("role", "mainboard")

    dc = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == event.card_scryfall_id,
            DeckCard.role == role,
        )
    )).first()
    if not dc:
        raise UndoNotSupported("La carta ya no está en el mazo")

    if stacked:
        # Revertir solo la cantidad añadida. Si eso deja quantity<=0, borramos.
        new_qty = dc.quantity - added_qty
        if new_qty <= 0:
            await db.delete(dc)
            return {
                "summary": f"Deshecho: {event.card_name} eliminada del mazo",
                "payload_extras": {"quantity": added_qty, "role": role},
            }
        dc.quantity = new_qty
        return {
            "summary": f"Deshecho: {event.card_name} vuelve a {new_qty}×",
            "payload_extras": {"old_qty": dc.quantity + added_qty, "new_qty": new_qty},
        }

    # No stacked: la carta fue creada por ese add. La borramos entera.
    if dc.quantity != added_qty:
        raise UndoNotSupported(
            f"La cantidad ha cambiado desde entonces ({added_qty} → {dc.quantity})"
        )
    await db.delete(dc)
    return {
        "summary": f"Deshecho: {event.card_name} eliminada de {role}",
        "payload_extras": {"quantity": added_qty, "role": role},
    }


async def _undo_card_removed(
    db: AsyncSession, event: DeckActivity, payload: dict, deck: Deck,
) -> dict[str, Any]:
    # Deshacer un remove = re-crear la carta con los datos del snapshot.
    # Si ya existe (el usuario la ha vuelto a añadir a mano), fallamos.
    quantity = payload.get("quantity", 1)
    role = payload.get("role", "mainboard")

    if not event.card_scryfall_id or not event.card_name:
        raise UndoNotSupported("Snapshot incompleto de la carta")

    existing = (await db.scalars(
        select(DeckCard).where(
            DeckCard.deck_id == deck.id,
            DeckCard.scryfall_id == event.card_scryfall_id,
            DeckCard.role == role,
        )
    )).first()
    if existing:
        raise UndoNotSupported(
            f"'{event.card_name}' ya está en {role} — no se puede duplicar"
        )

    # El printing debería seguir en cache (no lo borramos nunca).
    printing = await db.get(PrintingCache, event.card_scryfall_id)
    if not printing:
        raise UndoNotSupported("La impresión ya no está en caché")

    dc = DeckCard(
        deck_id=deck.id,
        oracle_id=event.card_oracle_id or "",
        name=event.card_name,
        quantity=quantity,
        scryfall_id=event.card_scryfall_id,
        role=role,
        include=True,
    )
    db.add(dc)
    return {
        "summary": f"Restaurada: {event.card_name} ({quantity}× en {role})",
        "payload_extras": {"quantity": quantity, "role": role},
    }


_HANDLERS: dict[str, Any] = {
    K.CARD_MOVED: _undo_card_moved,
    K.CARD_QTY_CHANGED: _undo_card_qty_changed,
    K.CARD_INCLUDE_TOGGLED: _undo_card_include_toggled,
    K.CARD_ART_CHANGED: _undo_card_art_changed,
    K.DECK_RENAMED: _undo_deck_renamed,
    K.CARD_ADDED: _undo_card_added,
    K.CARD_REMOVED: _undo_card_removed,
}


# Mapping para el kind del evento generado por el undo. Emitimos el kind
# "opuesto" para que se pinte con el icono correcto en el timeline.
_INVERSE_KIND: dict[str, str] = {
    K.CARD_ADDED: K.CARD_REMOVED,
    K.CARD_REMOVED: K.CARD_ADDED,
    K.CARD_MOVED: K.CARD_MOVED,
    K.CARD_QTY_CHANGED: K.CARD_QTY_CHANGED,
    K.CARD_INCLUDE_TOGGLED: K.CARD_INCLUDE_TOGGLED,
    K.CARD_ART_CHANGED: K.CARD_ART_CHANGED,
    K.DECK_RENAMED: K.DECK_RENAMED,
}
