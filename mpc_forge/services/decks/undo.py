from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.errors import CodedError
from mpc_forge.models import CustomArt, Deck, DeckActivity, DeckCard, PrintingCache
from mpc_forge.services.decks import deck_activity
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


UNDOABLE_KINDS: set[str] = {
    K.CARD_MOVED,
    K.CARD_QTY_CHANGED,
    K.CARD_INCLUDE_TOGGLED,
    K.CARD_ART_CHANGED,
    K.CUSTOM_ART_APPLIED,
    K.DECK_RENAMED,
    K.CARD_ADDED,
    K.CARD_REMOVED,
}


class UndoNotSupported(CodedError):
    code = "undo_conflict"


async def can_undo(event: DeckActivity) -> tuple[bool, str]:
    if event.kind not in UNDOABLE_KINDS:
        return False, "undo_kind_not_reversible"

    try:
        payload = json.loads(event.payload_json or "{}")
    except (ValueError, TypeError):
        return False, "undo_payload_corrupt"

    if event.kind == K.CARD_ART_CHANGED:
        if payload.get("kind") == "custom":
            return False, "undo_custom_art"
        if not payload.get("old_scryfall_id"):
            return False, "undo_missing_previous_art"
    if event.kind == K.CUSTOM_ART_APPLIED and not payload.get("changes"):
        return False, "undo_no_changes_saved"

    return True, ""


async def undo_event(db: AsyncSession, event: DeckActivity) -> dict[str, Any]:
    ok, reason = await can_undo(event)
    if not ok:
        raise UndoNotSupported(code=reason)

    payload = json.loads(event.payload_json or "{}")
    deck_id = event.deck_id
    if deck_id is None:
        raise UndoNotSupported(code="undo_no_deck")

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise UndoNotSupported(code="undo_deck_gone")

    kind = event.kind
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise UndoNotSupported(code="undo_kind_not_reversible", kind=kind)

    summary = await handler(db, event, payload, deck)

    inverse_kind = _INVERSE_KIND.get(kind, "undone")
    await deck_activity.log_event(
        db,
        deck_id,
        inverse_kind,
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


async def _undo_card_moved(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    from_role = payload.get("from_role")
    to_role = payload.get("to_role")
    if not from_role or not to_role:
        raise UndoNotSupported(code="undo_incomplete_snapshot")

    dc = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == event.card_scryfall_id,
                DeckCard.role == to_role,
            )
        )
    ).first()
    if not dc:
        raise UndoNotSupported(code="undo_card_moved_again", role=to_role)
    dc.role = from_role
    return {
        "summary": f"Deshecho: {event.card_name} vuelve a {from_role}",
        "payload_extras": {"from_role": to_role, "to_role": from_role},
    }


async def _undo_card_qty_changed(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    old_qty = payload.get("old_qty")
    new_qty = payload.get("new_qty")
    if old_qty is None or new_qty is None:
        raise UndoNotSupported(code="undo_incomplete_snapshot")

    dc = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == event.card_scryfall_id,
            )
        )
    ).first()
    if not dc:
        raise UndoNotSupported(code="undo_card_gone")
    if dc.quantity != new_qty:
        raise UndoNotSupported(code="undo_quantity_changed", expected=new_qty, current=dc.quantity)
    dc.quantity = old_qty
    return {
        "summary": f"Deshecho: {event.card_name} vuelve a {old_qty}×",
        "payload_extras": {"old_qty": new_qty, "new_qty": old_qty},
    }


async def _undo_card_include_toggled(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    prev_include = not payload.get("new_include", True)
    dc = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == event.card_scryfall_id,
            )
        )
    ).first()
    if not dc:
        raise UndoNotSupported(code="undo_card_gone")
    dc.include = prev_include
    label = "reactivada" if prev_include else "excluida"
    return {
        "summary": f"Deshecho: {event.card_name} {label}",
        "payload_extras": {"new_include": prev_include},
    }


async def _undo_card_art_changed(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    old_sfid = payload.get("old_scryfall_id")
    new_sfid = payload.get("new_scryfall_id")
    if not old_sfid or not new_sfid:
        raise UndoNotSupported(code="undo_incomplete_snapshot")

    dc = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == new_sfid,
            )
        )
    ).first()
    if not dc:
        raise UndoNotSupported(code="undo_art_changed")

    old_printing = await db.get(PrintingCache, old_sfid)
    if not old_printing:
        raise UndoNotSupported(code="undo_printing_not_cached")

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


async def _undo_custom_art_applied(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = payload.get("changes") or []
    cards = {
        c.id: c for c in (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck.id)))
    }
    old_ids = {c["old"] for c in changes if c.get("old") is not None}
    alive = set(
        (await db.scalars(select(CustomArt.id).where(CustomArt.id.in_(old_ids)))).all()
        if old_ids
        else ()
    )

    reverted: list[dict[str, Any]] = []
    for change in changes:
        card = cards.get(int(change.get("deck_card_id") or 0))
        if card is None:
            continue
        attr = "custom_art_back_id" if change.get("face") == "back" else "custom_art_front_id"
        if getattr(card, attr) != change.get("new"):
            continue
        old = change.get("old")
        restored = old if old in alive else None
        setattr(card, attr, restored)
        reverted.append({**change, "old": change.get("new"), "new": restored})

    if not reverted:
        raise UndoNotSupported(code="undo_art_changed")
    skipped = len(changes) - len(reverted)
    note = f" ({skipped} ya cambiadas, sin tocar)" if skipped else ""
    return {
        "summary": f"Deshecho: arte custom de {len(reverted)} cartas{note}",
        "payload_extras": {"count": len(reverted), "changes": reverted},
    }


async def _undo_deck_renamed(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    old_name = payload.get("old_name")
    new_name = payload.get("new_name")
    if not old_name:
        raise UndoNotSupported(code="undo_incomplete_snapshot")
    if deck.name != new_name:
        raise UndoNotSupported(code="undo_renamed_again", name=new_name)
    deck.name = old_name
    return {
        "summary": f"Deshecho: mazo vuelve a llamarse '{old_name}'",
        "payload_extras": {"old_name": new_name, "new_name": old_name},
    }


async def _undo_card_added(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    stacked = payload.get("stacked", False)
    added_qty = payload.get("quantity", 1)
    role = payload.get("role", "mainboard")

    dc = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == event.card_scryfall_id,
                DeckCard.role == role,
            )
        )
    ).first()
    if not dc:
        raise UndoNotSupported(code="undo_card_gone")

    if stacked:
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

    if dc.quantity != added_qty:
        raise UndoNotSupported(
            code="undo_quantity_changed", expected=added_qty, current=dc.quantity
        )
    await db.delete(dc)
    return {
        "summary": f"Deshecho: {event.card_name} eliminada de {role}",
        "payload_extras": {"quantity": added_qty, "role": role},
    }


async def _undo_card_removed(
    db: AsyncSession,
    event: DeckActivity,
    payload: dict[str, Any],
    deck: Deck,
) -> dict[str, Any]:
    quantity = payload.get("quantity", 1)
    role = payload.get("role", "mainboard")

    if not event.card_scryfall_id or not event.card_name:
        raise UndoNotSupported(code="undo_incomplete_snapshot")

    existing = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck.id,
                DeckCard.scryfall_id == event.card_scryfall_id,
                DeckCard.role == role,
            )
        )
    ).first()
    if existing:
        raise UndoNotSupported(code="undo_card_already_in", card=event.card_name, role=role)

    printing = await db.get(PrintingCache, event.card_scryfall_id)
    if not printing:
        raise UndoNotSupported(code="undo_printing_not_cached")

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


_UndoHandler = Callable[
    [AsyncSession, DeckActivity, dict[str, Any], Deck], Awaitable[dict[str, Any]]
]

_HANDLERS: dict[str, _UndoHandler] = {
    K.CARD_MOVED: _undo_card_moved,
    K.CARD_QTY_CHANGED: _undo_card_qty_changed,
    K.CARD_INCLUDE_TOGGLED: _undo_card_include_toggled,
    K.CARD_ART_CHANGED: _undo_card_art_changed,
    K.CUSTOM_ART_APPLIED: _undo_custom_art_applied,
    K.DECK_RENAMED: _undo_deck_renamed,
    K.CARD_ADDED: _undo_card_added,
    K.CARD_REMOVED: _undo_card_removed,
}


_INVERSE_KIND: dict[str, str] = {
    K.CARD_ADDED: K.CARD_REMOVED,
    K.CARD_REMOVED: K.CARD_ADDED,
    K.CARD_MOVED: K.CARD_MOVED,
    K.CARD_QTY_CHANGED: K.CARD_QTY_CHANGED,
    K.CARD_INCLUDE_TOGGLED: K.CARD_INCLUDE_TOGGLED,
    K.CARD_ART_CHANGED: K.CARD_ART_CHANGED,
    K.CUSTOM_ART_APPLIED: K.CUSTOM_ART_APPLIED,
    K.DECK_RENAMED: K.DECK_RENAMED,
}
