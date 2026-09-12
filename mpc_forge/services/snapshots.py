"""Snapshots de mazo: fotos congeladas que se pueden restaurar.

``DeckActivity`` ya registraba evento a evento y permitía deshacer uno concreto,
pero no respondía a "devuélvelo a como estaba antes del torneo". Deshacer
veinte cambios uno a uno no es lo mismo que volver a un punto conocido.

Un snapshot serializa la lista completa —cartas, cantidades, roles, artes
elegidos— en un JSON y permite restaurarla o compararla con otra.

Automáticos vs manuales
-----------------------
Las operaciones masivas (aplicar un tema de arte, localizar el mazo entero a
otro idioma) crean un snapshot automático antes de tocar nada. Esos se podan:
se conservan los ``MAX_AUTO_SNAPSHOTS`` más recientes por mazo. Los que crea el
usuario a mano no se borran nunca — son suyos.

Por qué JSON y no filas
-----------------------
Un snapshot es un valor inmutable que solo se lee entero. Normalizarlo en una
tabla de "cartas del snapshot" añadiría un join y una FK por cada restauración
sin ganar nada: nunca se consulta "en qué snapshots aparece Sol Ring".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckCard, DeckSnapshot

log = logging.getLogger(__name__)

# Cuántos snapshots automáticos se conservan por mazo.
MAX_AUTO_SNAPSHOTS = 10

# Versión del formato de `payload_json`. Si algún día cambia la estructura,
# `restore` puede migrar los antiguos en vez de fallar en silencio.
PAYLOAD_VERSION = 1


@dataclass
class DiffEntry:
    name: str
    change: str          # added | removed | quantity | art | role
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "change": self.change,
            "before": self.before, "after": self.after,
        }


def _serialize_card(card: DeckCard) -> dict[str, Any]:
    return {
        "oracle_id": card.oracle_id,
        "name": card.name,
        "quantity": card.quantity,
        "scryfall_id": card.scryfall_id,
        "custom_art_front_id": card.custom_art_front_id,
        "custom_art_back_id": card.custom_art_back_id,
        "role": card.role,
        "include": card.include,
    }


async def create(
    db: AsyncSession,
    deck_id: int,
    *,
    label: str = "",
    auto: bool = False,
) -> dict[str, Any] | None:
    """Congela el estado actual del mazo."""
    deck = await db.get(Deck, deck_id)
    if deck is None:
        return None

    cards = (await db.execute(
        select(DeckCard).where(DeckCard.deck_id == deck_id).order_by(DeckCard.name)
    )).scalars().all()

    payload = {
        "version": PAYLOAD_VERSION,
        "deck_name": deck.name,
        "format": getattr(deck, "format", ""),
        "cards": [_serialize_card(c) for c in cards],
    }

    snapshot = DeckSnapshot(
        deck_id=deck_id,
        label=label.strip() or _default_label(auto),
        card_count=sum(c.quantity for c in cards),
        auto=auto,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(snapshot)
    await db.commit()

    if auto:
        await prune_auto(db, deck_id)

    log.info("Snapshot '%s' creado para el mazo %d (%d cartas)",
             snapshot.label, deck_id, snapshot.card_count)
    return _snapshot_to_dict(snapshot)


def _default_label(auto: bool) -> str:
    stamp = datetime.now(UTC).strftime("%d/%m/%Y %H:%M")
    return f"Automático {stamp}" if auto else f"Guardado {stamp}"


def _snapshot_to_dict(s: DeckSnapshot) -> dict[str, Any]:
    return {
        "id": s.id,
        "deck_id": s.deck_id,
        "label": s.label,
        "card_count": s.card_count,
        "auto": s.auto,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


async def list_for_deck(db: AsyncSession, deck_id: int) -> list[dict[str, Any]]:
    rows = (await db.execute(
        select(DeckSnapshot)
        .where(DeckSnapshot.deck_id == deck_id)
        .order_by(DeckSnapshot.created_at.desc())
    )).scalars().all()
    return [_snapshot_to_dict(s) for s in rows]


async def prune_auto(db: AsyncSession, deck_id: int) -> int:
    """Borra los snapshots automáticos más antiguos. Devuelve cuántos."""
    rows = (await db.execute(
        select(DeckSnapshot.id)
        .where(DeckSnapshot.deck_id == deck_id, DeckSnapshot.auto.is_(True))
        .order_by(DeckSnapshot.created_at.desc())
    )).scalars().all()

    stale = list(rows)[MAX_AUTO_SNAPSHOTS:]
    if not stale:
        return 0
    await db.execute(delete(DeckSnapshot).where(DeckSnapshot.id.in_(stale)))
    await db.commit()
    return len(stale)


async def restore(
    db: AsyncSession, snapshot_id: int, *, keep_current_as: str = ""
) -> dict[str, Any] | None:
    """Devuelve el mazo al estado del snapshot.

    Antes de sobrescribir se guarda el estado actual como snapshot automático:
    restaurar por error no debe ser un callejón sin salida.

    Las cartas se reemplazan por completo en vez de intentar un merge. Un
    merge tendría que decidir qué hacer con las cartas añadidas después del
    snapshot, y cualquier respuesta sorprendería a alguien; "vuelve exactamente
    a como estaba" no tiene ambigüedad.
    """
    snapshot = await db.get(DeckSnapshot, snapshot_id)
    if snapshot is None or snapshot.deck_id is None:
        return None
    deck = await db.get(Deck, snapshot.deck_id)
    if deck is None:
        return None

    try:
        payload = json.loads(snapshot.payload_json)
    except json.JSONDecodeError:
        log.exception("El snapshot %d tiene un payload ilegible", snapshot_id)
        return None

    if payload.get("version") != PAYLOAD_VERSION:
        log.warning(
            "El snapshot %d es de la versión %s y esta build espera la %s; "
            "se intenta restaurar igualmente.",
            snapshot_id, payload.get("version"), PAYLOAD_VERSION,
        )

    await create(
        db, deck.id,
        label=keep_current_as or f"Antes de restaurar «{snapshot.label}»",
        auto=True,
    )

    await db.execute(delete(DeckCard).where(DeckCard.deck_id == deck.id))
    for card in payload.get("cards", []):
        db.add(DeckCard(
            deck_id=deck.id,
            oracle_id=card.get("oracle_id", ""),
            name=card.get("name", ""),
            quantity=int(card.get("quantity", 1)),
            scryfall_id=card.get("scryfall_id", ""),
            custom_art_front_id=card.get("custom_art_front_id"),
            custom_art_back_id=card.get("custom_art_back_id"),
            role=card.get("role", "mainboard"),
            include=bool(card.get("include", True)),
        ))
    await db.commit()

    log.info("Mazo %d restaurado al snapshot %d ('%s')",
             deck.id, snapshot_id, snapshot.label)
    return {
        "deck_id": deck.id,
        "snapshot_id": snapshot_id,
        "label": snapshot.label,
        "restored_cards": len(payload.get("cards", [])),
    }


async def diff(
    db: AsyncSession, snapshot_id: int, other_snapshot_id: int | None = None
) -> dict[str, Any] | None:
    """Compara un snapshot con otro, o con el estado actual del mazo."""
    base = await db.get(DeckSnapshot, snapshot_id)
    if base is None or base.deck_id is None:
        return None

    try:
        base_cards = json.loads(base.payload_json).get("cards", [])
    except json.JSONDecodeError:
        return None

    if other_snapshot_id is not None:
        other = await db.get(DeckSnapshot, other_snapshot_id)
        if other is None:
            return None
        try:
            other_cards = json.loads(other.payload_json).get("cards", [])
        except json.JSONDecodeError:
            return None
        other_label = other.label
    else:
        rows = (await db.execute(
            select(DeckCard).where(DeckCard.deck_id == base.deck_id)
        )).scalars().all()
        other_cards = [_serialize_card(c) for c in rows]
        other_label = "Estado actual"

    entries = _diff_lists(base_cards, other_cards)
    return {
        "from": {"id": base.id, "label": base.label},
        "to": {"id": other_snapshot_id, "label": other_label},
        "changes": [e.to_dict() for e in entries],
        "summary": {
            "added": sum(1 for e in entries if e.change == "added"),
            "removed": sum(1 for e in entries if e.change == "removed"),
            "quantity": sum(1 for e in entries if e.change == "quantity"),
            "art": sum(1 for e in entries if e.change == "art"),
            "role": sum(1 for e in entries if e.change == "role"),
        },
    }


def _diff_lists(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[DiffEntry]:
    """Compara dos listas de cartas serializadas.

    Se indexa por ``oracle_id`` y no por nombre: el mazo puede haberse
    localizado a otro idioma entre los dos estados, y entonces todas las cartas
    saldrían como "eliminada" y "añadida" pese a ser las mismas.
    """
    def index(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for card in cards:
            key = card.get("oracle_id") or card.get("name", "")
            if key in out:
                # Copias con artes distintos: se suman las cantidades y gana la
                # primera para el resto de campos.
                out[key]["quantity"] += card.get("quantity", 0)
            else:
                out[key] = dict(card)
        return out

    a, b = index(before), index(after)
    entries: list[DiffEntry] = []

    for key in a.keys() - b.keys():
        entries.append(DiffEntry(a[key].get("name", key), "removed",
                                 before=a[key].get("quantity")))
    for key in b.keys() - a.keys():
        entries.append(DiffEntry(b[key].get("name", key), "added",
                                 after=b[key].get("quantity")))

    for key in a.keys() & b.keys():
        old, new = a[key], b[key]
        name = new.get("name", key)
        if old.get("quantity") != new.get("quantity"):
            entries.append(DiffEntry(name, "quantity",
                                     old.get("quantity"), new.get("quantity")))
        if (old.get("scryfall_id") != new.get("scryfall_id")
                or old.get("custom_art_front_id") != new.get("custom_art_front_id")):
            entries.append(DiffEntry(name, "art",
                                     old.get("scryfall_id"), new.get("scryfall_id")))
        if old.get("role") != new.get("role"):
            entries.append(DiffEntry(name, "role",
                                     old.get("role"), new.get("role")))

    entries.sort(key=lambda e: (e.change, e.name))
    return entries


async def delete_snapshot(db: AsyncSession, snapshot_id: int) -> bool:
    snapshot = await db.get(DeckSnapshot, snapshot_id)
    if snapshot is None:
        return False
    await db.delete(snapshot)
    await db.commit()
    return True
