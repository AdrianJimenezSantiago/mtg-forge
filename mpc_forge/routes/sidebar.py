from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from mpc_forge.models import CollectionEntry, Deck, DeckCard, PrintRun
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.decks import deck_covers

router = APIRouter(prefix="/api/collection", tags=["collection"])

RECENT_DECKS_LIMIT = 5


class SidebarStats(BaseModel):
    total_decks: int = 0
    unique_cards: int = 0
    total_print_runs: int = 0
    collection_total: int = 0


@router.get("/sidebar-stats", response_model=SidebarStats)
async def sidebar_stats(db: DbDep) -> SidebarStats:
    row = (
        await db.execute(
            select(
                select(func.count()).select_from(Deck).scalar_subquery(),
                select(func.count(func.distinct(DeckCard.oracle_id))).scalar_subquery(),
                select(func.count()).select_from(PrintRun).scalar_subquery(),
                select(func.count()).select_from(CollectionEntry).scalar_subquery(),
            )
        )
    ).one()
    decks, unique, runs, collection = (int(v or 0) for v in row)
    return SidebarStats(
        total_decks=decks,
        unique_cards=unique,
        total_print_runs=runs,
        collection_total=collection,
    )


class RecentDeck(BaseModel):
    id: int
    name: str
    format: str
    card_count: int = 0
    commander_image: str | None = None


@router.get("/recent-decks", response_model=list[RecentDeck])
async def recent_decks(db: DbDep) -> list[RecentDeck]:
    return [
        RecentDeck(
            id=deck.id,
            name=deck.name,
            format=deck.format,
            card_count=count,
            commander_image=cover.image_url,
        )
        for deck, count, cover in await deck_covers.decks_with_covers(db, RECENT_DECKS_LIMIT)
    ]
