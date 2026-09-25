"""Portada de un mazo: el arte que el mazo usa de verdad para su commander.

Antes cada vista calculaba la portada con ``Deck.commander_scryfall_id``, que
es la impresión con la que se IMPORTÓ el commander. Si después el usuario
elegía otro arte (otra impresión de Scryfall, un arte de un drive o una imagen
propia), la biblioteca, la landing, el historial y la barra lateral seguían
enseñando el arte original.

Aquí se resuelve una sola vez, con la misma regla que usa el editor para la
miniatura de cada carta (``routes/decks/_views.py``):

1. arte custom de la cara frontal, si la carta tiene uno;
2. si no, la impresión elegida para ESA carta del mazo;
3. si el mazo no tiene carta de commander (datos antiguos, commander
   borrado), la impresión de ``Deck.commander_scryfall_id`` como hasta ahora.

¿Qué carta es "el commander"? Con compañeros (partner, background) hay dos
cartas con rol ``commander``. Se elige la que corresponde a
``Deck.commander_scryfall_id`` (por impresión o, si ya se cambió el arte, por
``oracle_id``), y si no se puede determinar, la primera por id. El editor usa
``cover_card_id`` para seguir a la misma carta cuando se cambia su arte.

Todas las consultas van por lotes (``IN``), troceadas para no pasar el límite
de variables de SQLite: la biblioteca pide portadas de todos los mazos a la
vez.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.services import custom_art

T = TypeVar("T")

_IN_CHUNK = 500

COMMANDER_ROLE = "commander"


@dataclass(frozen=True)
class DeckCover:
    """Portada resuelta de un mazo."""

    image_url: str | None
    name: str | None
    card_id: int | None = None


EMPTY = DeckCover(image_url=None, name=None, card_id=None)


def _chunks(items: Iterable[T], size: int = _IN_CHUNK) -> Iterable[list[T]]:
    seq = list(items)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _printing_image(p: PrintingCache | None) -> str | None:
    if p is None:
        return None
    return p.image_normal or p.image_large


def pick_cover_card(
    deck: Deck,
    commanders: Sequence[DeckCard],
    printings: dict[str, PrintingCache],
) -> DeckCard | None:
    """De las cartas con rol commander, la que representa al mazo.

    ``commanders`` debe venir ordenada por id (orden de importación).
    """
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


async def covers_for_decks(
    db: AsyncSession, decks: Sequence[Deck]
) -> dict[int, DeckCover]:
    """Portada de cada mazo, indexada por ``deck.id``. Tres consultas en total."""
    if not decks:
        return {}
    deck_ids = [d.id for d in decks]

    commanders_by_deck: dict[int, list[DeckCard]] = {}
    for chunk in _chunks(deck_ids):
        rows = (await db.scalars(
            select(DeckCard)
            .where(DeckCard.deck_id.in_(chunk), DeckCard.role == COMMANDER_ROLE)
            .order_by(DeckCard.id)
        )).all()
        for card in rows:
            commanders_by_deck.setdefault(card.deck_id, []).append(card)

    printing_ids: set[str] = {d.commander_scryfall_id for d in decks if d.commander_scryfall_id}
    for cards in commanders_by_deck.values():
        printing_ids.update(c.scryfall_id for c in cards if c.scryfall_id)
    printings: dict[str, PrintingCache] = {}
    for chunk in _chunks(printing_ids):
        rows = (await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk))
        )).all()
        printings.update({p.scryfall_id: p for p in rows})

    chosen: dict[int, DeckCard | None] = {
        d.id: pick_cover_card(d, commanders_by_deck.get(d.id, []), printings)
        for d in decks
    }
    custom_ids = {c.custom_art_front_id for c in chosen.values() if c and c.custom_art_front_id}
    customs: dict[int, CustomArt] = {}
    for chunk in _chunks(custom_ids):
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
    """Atajo para un solo mazo."""
    return (await covers_for_decks(db, [deck])).get(deck.id, EMPTY)
