from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import Deck, DeckCard
from mpc_forge.services.art.art_cache import ArtCache
from mpc_forge.services.printing import build_progress
from mpc_forge.services.printing.xml_generator import DeckCardResolved, resolve_deck_for_xml

DECKLIST_FORMATS = frozenset({"simple", "with_set", "arena"})


def make_router() -> APIRouter:
    return APIRouter(prefix="/api", tags=["export"])


async def get_deck_or_404(db: AsyncSession, deck_id: int) -> Deck:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return deck


def export_path(deck_name: str, suffix: str) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return cfg.PATHS.exports_dir / f"{slugify(deck_name)}-{stamp}{suffix}"


async def resolve_with_progress(
    db: AsyncSession,
    scryfall: ScryfallClient,
    art_cache: ArtCache,
    deck: Deck,
    kind: str,
) -> list[DeckCardResolved]:
    total = (
        await db.scalar(
            select(func.count(DeckCard.id)).where(
                DeckCard.deck_id == deck.id, DeckCard.include.is_(True)
            )
        )
    ) or 0
    build_progress.start(deck.id, total, kind=kind)
    try:
        resolved = await resolve_deck_for_xml(
            db,
            scryfall,
            art_cache,
            deck,
            on_progress=lambda name: build_progress.tick(deck.id, name),
        )
    except Exception as e:
        build_progress.finish(deck.id, error=str(e))
        raise
    if not resolved:
        build_progress.finish(deck.id, error="Mazo sin cartas resueltas")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")
    return resolved
