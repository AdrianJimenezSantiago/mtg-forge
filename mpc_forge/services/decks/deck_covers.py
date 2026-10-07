from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.services.art import custom_art
from mpc_forge.utils.iterables import chunked

T = TypeVar("T")


COMMANDER_ROLE = "commander"


@dataclass(frozen=True)
class DeckCover:
    image_url: str | None
    name: str | None
    card_id: int | None = None


EMPTY = DeckCover(image_url=None, name=None, card_id=None)


def _printing_image(p: PrintingCache | None) -> str | None:
    if p is None:
        return None
    return p.image_normal or p.image_large


def pick_cover_card(
    deck: Deck,
    commanders: Sequence[DeckCard],
    printings: dict[str, PrintingCache],
) -> DeckCard | None:
    if not commanders:
        return None
    wanted = deck.commander_scryfall_id
    if wanted:
        for card in commanders:
            if card.scryfall_id == wanted:
                return card
        base = printings.get(wanted)
        if base is not None and base.oracle_id:
            for card in commanders:
                if card.oracle_id == base.oracle_id:
                    return card
    return commanders[0]


async def covers_for_decks(db: AsyncSession, decks: Sequence[Deck]) -> dict[int, DeckCover]:
    if not decks:
        return {}
    deck_ids = [d.id for d in decks]

    commanders_by_deck: dict[int, list[DeckCard]] = {}
    for chunk in chunked(deck_ids):
        rows = (
            await db.scalars(
                select(DeckCard)
                .where(DeckCard.deck_id.in_(chunk), DeckCard.role == COMMANDER_ROLE)
                .order_by(DeckCard.id)
            )
        ).all()
        for card in rows:
            commanders_by_deck.setdefault(card.deck_id, []).append(card)

    printing_ids: set[str] = {d.commander_scryfall_id for d in decks if d.commander_scryfall_id}
    for cards in commanders_by_deck.values():
        printing_ids.update(c.scryfall_id for c in cards if c.scryfall_id)
    printings: dict[str, PrintingCache] = {}
    for chunk in chunked(printing_ids):
        rows = (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all()
        printings.update({p.scryfall_id: p for p in rows})

    chosen: dict[int, DeckCard | None] = {
        d.id: pick_cover_card(d, commanders_by_deck.get(d.id, []), printings) for d in decks
    }
    custom_ids = {c.custom_art_front_id for c in chosen.values() if c and c.custom_art_front_id}
    customs: dict[int, CustomArt] = {}
    for chunk in chunked(custom_ids):
        rows = (await db.scalars(select(CustomArt).where(CustomArt.id.in_(chunk)))).all()
        customs.update({ca.id: ca for ca in rows})

    out: dict[int, DeckCover] = {}
    for deck in decks:
        card = chosen[deck.id]
        fallback = printings.get(deck.commander_scryfall_id) if deck.commander_scryfall_id else None
        if card is None:
            out[deck.id] = DeckCover(
                image_url=_printing_image(fallback),
                name=fallback.name if fallback else None,
            )
            continue

        image: str | None = None
        if card.custom_art_front_id:
            ca = customs.get(card.custom_art_front_id)
            if ca is not None:
                image = custom_art.custom_art_url(ca.relative_path)
        if image is None:
            image = _printing_image(printings.get(card.scryfall_id))
        if image is None:
            image = _printing_image(fallback)
        out[deck.id] = DeckCover(image_url=image, name=card.name, card_id=card.id)
    return out


async def cover_for_deck(db: AsyncSession, deck: Deck) -> DeckCover:
    return (await covers_for_decks(db, [deck])).get(deck.id, EMPTY)


async def decks_with_covers(
    db: AsyncSession, limit: int | None = None
) -> list[tuple[Deck, int, DeckCover]]:
    stmt = (
        select(Deck, func.count(DeckCard.id).label("card_count"))
        .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
        .group_by(Deck.id)
        .order_by(Deck.updated_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    covers = await covers_for_decks(db, [deck for deck, _ in rows])
    return [(deck, count, covers.get(deck.id, EMPTY)) for deck, count in rows]
