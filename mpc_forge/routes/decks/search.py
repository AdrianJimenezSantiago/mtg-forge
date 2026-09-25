"""Búsqueda de cartas a través de todos los mazos del usuario.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select

from mpc_forge.models import (
    Deck,
    DeckCard,
    PrintingCache,
)

log = logging.getLogger(__name__)


from mpc_forge.routes.decks._common import (
    DbDep,
    make_router,
)

router = make_router()


class CardInDeck(BaseModel):
    """Una instancia concreta de una carta dentro de un mazo del usuario."""
    deck_card_id: int
    deck_id: int
    deck_name: str
    deck_format: str
    role: str
    quantity: int
    scryfall_id: str
    printing_set: str | None
    printing_number: str | None
    printing_lang: str | None
    has_custom_art: bool
    include: bool
    thumbnail: str | None


class CardSearchGroup(BaseModel):
    """Agrupación por oracle_id — todas las copias de "la misma carta"
    en todos los mazos, incluyendo distintas impresiones."""
    oracle_id: str
    canonical_name: str
    total_copies: int
    deck_count: int
    instances: list[CardInDeck]


class CardSearchResponse(BaseModel):
    query: str
    total_groups: int
    total_instances: int
    groups: list[CardSearchGroup]


@router.get("/_/search-cards", response_model=CardSearchResponse)
async def search_cards_across_decks(
    q: str,
    db: DbDep,
    limit: int = 200,
) -> CardSearchResponse:
    """Busca cartas por nombre (substring, case-insensitive) en TODOS los mazos.

    Devuelve resultados agrupados por ``oracle_id`` para que "Sol Ring" salga
    una sola vez con todas las instancias que hay en distintos mazos
    (posiblemente con impresiones distintas). Incluye la impresión concreta
    elegida en cada instancia para que el frontend pueda mostrar la thumbnail.

    - Sin resultados si ``q`` tiene menos de 2 caracteres útiles.
    - Los tokens y meld_result se INCLUYEN — a veces quieres saber en qué
      mazos tienes generado un token concreto.
    """
    q_clean = (q or "").strip()
    if len(q_clean) < 2:
        return CardSearchResponse(query=q_clean, total_groups=0, total_instances=0, groups=[])

    pattern = f"%{q_clean}%"
    limit = max(1, min(limit, 500))

    rows = (
        await db.execute(
            select(DeckCard, Deck, PrintingCache)
            .join(Deck, Deck.id == DeckCard.deck_id)
            .join(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id, isouter=True)
            .where(DeckCard.name.ilike(pattern))
            .order_by(DeckCard.name, Deck.updated_at.desc())
            .limit(limit)
        )
    ).all()

    groups_map: dict[str, CardSearchGroup] = {}
    for dc, deck, printing in rows:
        key = dc.oracle_id or f"name:{dc.name.lower()}"
        instance = CardInDeck(
            deck_card_id=dc.id,
            deck_id=deck.id,
            deck_name=deck.name,
            deck_format=deck.format,
            role=dc.role,
            quantity=dc.quantity,
            scryfall_id=dc.scryfall_id,
            printing_set=printing.set_code if printing else None,
            printing_number=printing.collector_number if printing else None,
            printing_lang=printing.lang if printing else None,
            has_custom_art=bool(dc.custom_art_front_id),
            include=dc.include,
            thumbnail=printing.image_normal if printing else None,
        )
        if key not in groups_map:
            groups_map[key] = CardSearchGroup(
                oracle_id=dc.oracle_id or "",
                canonical_name=dc.name,
                total_copies=0,
                deck_count=0,
                instances=[],
            )
        groups_map[key].instances.append(instance)
        groups_map[key].total_copies += dc.quantity

    for group in groups_map.values():
        group.deck_count = len({inst.deck_id for inst in group.instances})

    q_lower = q_clean.lower()

    def _sort_key(g: CardSearchGroup) -> tuple[int, str]:
        starts = 0 if g.canonical_name.lower().startswith(q_lower) else 1
        return (starts, g.canonical_name.lower())

    ordered = sorted(groups_map.values(), key=_sort_key)

    return CardSearchResponse(
        query=q_clean,
        total_groups=len(ordered),
        total_instances=sum(len(g.instances) for g in ordered),
        groups=ordered,
    )
