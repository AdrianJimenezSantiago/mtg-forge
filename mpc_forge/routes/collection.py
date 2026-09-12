"""Endpoints REST para la colección personal (tracking por set).

Incluye cache en memoria para la lista de sets de Scryfall (TTL 1h)
para evitar llamadas redundantes al abrir la página de colección.
"""
from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session
from mpc_forge.models import CollectionEntry, Deck, DeckCard, PrintRun

router = APIRouter(prefix="/api/collection", tags=["collection"])
log = logging.getLogger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


# ── In-memory cache for Scryfall sets ──────────────────────────────────
# Scryfall's /sets endpoint rarely changes (new sets appear every few months).
# We cache the filtered+processed list for 1 hour to avoid hitting Scryfall
# on every page load. The cache stores the final list of SetInfo dicts
# (without owned_count — that's merged fresh from DB on each request).

_SETS_CACHE: dict[str, list[dict]] | None = None
_SETS_CACHE_AT: float = 0.0
_SETS_CACHE_TTL: float = 3600.0  # 1 hour

WANTED_SET_TYPES = frozenset({
    "core", "expansion", "masters", "draft_innovation",
    "commander", "funny", "starter",
})


async def _fetch_sets_cached(scryfall_client: ScryfallClient) -> list[dict]:
    """Return the filtered sets list, fetching from Scryfall only if stale."""
    global _SETS_CACHE, _SETS_CACHE_AT

    now = time.monotonic()
    if _SETS_CACHE is not None and (now - _SETS_CACHE_AT) < _SETS_CACHE_TTL:
        return _SETS_CACHE

    import httpx

    from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
    from mpc_forge.ssl_config import ssl_insecure

    log.info("Fetching sets list from Scryfall (cache miss / stale)")
    async with httpx.AsyncClient(
        base_url=SCRYFALL_API,
        headers={"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        verify=not ssl_insecure(),
    ) as client:
        resp = await client.get("/sets")
        resp.raise_for_status()
        data = resp.json()

    sets_raw = data.get("data", [])

    # Filter and pre-process into lightweight dicts
    filtered = []
    for s in sets_raw:
        if s.get("set_type") not in WANTED_SET_TYPES:
            continue
        if s.get("card_count", 0) <= 0:
            continue
        filtered.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "set_type": s.get("set_type", ""),
            "released_at": s.get("released_at"),
            "card_count": s.get("card_count", 0),
            "icon_svg_uri": s.get("icon_svg_uri"),
        })

    # Sort by release date descending
    filtered.sort(key=lambda x: x.get("released_at") or "", reverse=True)

    _SETS_CACHE = filtered
    _SETS_CACHE_AT = now
    log.info("Sets cache updated: %d sets", len(filtered))
    return filtered


# ── Sidebar stats ──────────────────────────────────────────────────────

class SidebarStats(BaseModel):
    total_decks: int = 0
    unique_cards: int = 0
    total_print_runs: int = 0
    collection_total: int = 0


@router.get("/sidebar-stats", response_model=SidebarStats)
async def sidebar_stats(db: DbDep) -> SidebarStats:
    """Estadísticas rápidas para el sidebar."""
    decks = (await db.scalar(select(func.count()).select_from(Deck))) or 0
    unique = (await db.scalar(
        select(func.count(func.distinct(DeckCard.oracle_id)))
    )) or 0
    runs = (await db.scalar(select(func.count()).select_from(PrintRun))) or 0
    coll = (await db.scalar(select(func.count()).select_from(CollectionEntry))) or 0
    return SidebarStats(
        total_decks=decks,
        unique_cards=unique,
        total_print_runs=runs,
        collection_total=coll,
    )


# ── Recent decks ───────────────────────────────────────────────────────

class RecentDeck(BaseModel):
    id: int
    name: str
    format: str
    card_count: int = 0
    commander_image: str | None = None


@router.get("/recent-decks", response_model=list[RecentDeck])
async def recent_decks(db: DbDep) -> list[RecentDeck]:
    """Últimos 5 mazos editados para acceso rápido en el sidebar."""
    from mpc_forge.models import PrintingCache

    result = await db.execute(
        select(Deck, func.count(DeckCard.id).label("cc"))
        .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
        .group_by(Deck.id)
        .order_by(Deck.updated_at.desc())
        .limit(5)
    )
    rows = result.all()
    if not rows:
        return []

    # Batch commander images
    cmd_ids = {d.commander_scryfall_id for d, _ in rows if d.commander_scryfall_id}
    imgs: dict[str, str | None] = {}
    if cmd_ids:
        prints = (await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(cmd_ids))
        )).all()
        imgs = {p.scryfall_id: p.image_normal for p in prints}

    out = []
    for deck, cc in rows:
        out.append(RecentDeck(
            id=deck.id,
            name=deck.name,
            format=deck.format,
            card_count=cc,
            commander_image=imgs.get(deck.commander_scryfall_id) if deck.commander_scryfall_id else None,
        ))
    return out


# ── Sets list (cached) ─────────────────────────────────────────────────

class SetInfo(BaseModel):
    code: str
    name: str
    set_type: str
    released_at: str | None = None
    card_count: int = 0
    icon_svg_uri: str | None = None
    owned_count: int = 0


@router.get("/sets")
async def list_sets(
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[SetInfo]:
    """Devuelve los sets de Scryfall con el conteo de cartas propias.

    La lista de sets se cachea en memoria 1h — solo la query de owned_count
    es fresca en cada request (una SELECT rápida contra SQLite).
    """
    sets_data = await _fetch_sets_cached(scryfall)

    # Owned counts from DB (always fresh)
    owned_q = await db.execute(
        select(CollectionEntry.set_code, func.count())
        .group_by(CollectionEntry.set_code)
    )
    owned_map = dict(owned_q.all())

    return [
        SetInfo(
            **s,
            owned_count=owned_map.get(s["code"], 0),
        )
        for s in sets_data
    ]


# ── Cards in a set ─────────────────────────────────────────────────────

class SetCardInfo(BaseModel):
    scryfall_id: str
    oracle_id: str
    name: str
    collector_number: str
    rarity: str
    image_small: str | None = None
    image_normal: str | None = None
    owned: bool = False


@router.get("/sets/{set_code}/cards")
async def set_cards(
    set_code: str,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[SetCardInfo]:
    """Devuelve las cartas de un set con el flag de si el usuario las tiene."""
    import httpx

    from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
    from mpc_forge.services.rate_limiter import AsyncRateLimiter
    from mpc_forge.ssl_config import ssl_insecure

    all_cards: list[dict] = []
    async with httpx.AsyncClient(
        base_url=SCRYFALL_API,
        headers={"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
        verify=not ssl_insecure(),
    ) as client:
        limiter = AsyncRateLimiter(0.10)
        params = {
            "q": f"set:{set_code} -is:digital",
            "unique": "prints",
            "order": "set",
        }
        await limiter.acquire()
        resp = await client.get("/cards/search", params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        page = resp.json()

        while page:
            all_cards.extend(page.get("data", []))
            if not page.get("has_more"):
                break
            next_url = page.get("next_page")
            if not next_url:
                break
            await limiter.acquire()
            resp = await client.get(next_url)
            resp.raise_for_status()
            page = resp.json()

    # Owned IDs for this set (single fast query)
    owned_ids = set(
        (await db.scalars(
            select(CollectionEntry.scryfall_id)
            .where(CollectionEntry.set_code == set_code)
        )).all()
    )

    out = []
    for c in all_cards:
        sid = c.get("id", "")
        imgs = c.get("image_uris") or {}
        if not imgs and c.get("card_faces"):
            imgs = c["card_faces"][0].get("image_uris", {})
        out.append(SetCardInfo(
            scryfall_id=sid,
            oracle_id=c.get("oracle_id", ""),
            name=c.get("name", ""),
            collector_number=c.get("collector_number", ""),
            rarity=c.get("rarity", "common"),
            image_small=imgs.get("small"),
            image_normal=imgs.get("normal"),
            owned=sid in owned_ids,
        ))

    return out


# ── Toggle owned ───────────────────────────────────────────────────────

class ToggleOwnedRequest(BaseModel):
    scryfall_id: str
    oracle_id: str
    name: str
    set_code: str
    set_name: str = ""
    collector_number: str = ""
    rarity: str = "common"
    image_small: str | None = None


class ToggleOwnedResponse(BaseModel):
    owned: bool
    set_owned_count: int


@router.post("/toggle-owned", response_model=ToggleOwnedResponse)
async def toggle_owned(payload: ToggleOwnedRequest, db: DbDep) -> ToggleOwnedResponse:
    """Marca/desmarca una carta como propia."""
    existing = await db.get(CollectionEntry, payload.scryfall_id)
    if existing:
        await db.delete(existing)
        await db.flush()
        owned = False
    else:
        entry = CollectionEntry(
            scryfall_id=payload.scryfall_id,
            oracle_id=payload.oracle_id,
            name=payload.name,
            set_code=payload.set_code,
            set_name=payload.set_name,
            collector_number=payload.collector_number,
            rarity=payload.rarity,
            image_small=payload.image_small,
        )
        db.add(entry)
        await db.flush()
        owned = True

    count = (await db.scalar(
        select(func.count())
        .select_from(CollectionEntry)
        .where(CollectionEntry.set_code == payload.set_code)
    )) or 0

    await db.commit()
    return ToggleOwnedResponse(owned=owned, set_owned_count=count)


# ── Batch toggle ───────────────────────────────────────────────────────

class BatchToggleRequest(BaseModel):
    set_code: str
    set_name: str = ""
    cards: list[ToggleOwnedRequest]
    action: str = "add"  # "add" | "remove"


class BatchToggleResponse(BaseModel):
    added: int = 0
    removed: int = 0
    set_owned_count: int = 0


@router.post("/batch-toggle", response_model=BatchToggleResponse)
async def batch_toggle(payload: BatchToggleRequest, db: DbDep) -> BatchToggleResponse:
    """Marca/desmarca múltiples cartas de golpe."""
    added = 0
    removed = 0

    if payload.action == "remove":
        ids = [c.scryfall_id for c in payload.cards]
        result = await db.execute(
            delete(CollectionEntry).where(CollectionEntry.scryfall_id.in_(ids))
        )
        removed = result.rowcount  # type: ignore
    else:
        existing = set((await db.scalars(
            select(CollectionEntry.scryfall_id).where(
                CollectionEntry.scryfall_id.in_([c.scryfall_id for c in payload.cards])
            )
        )).all())
        for c in payload.cards:
            if c.scryfall_id not in existing:
                db.add(CollectionEntry(
                    scryfall_id=c.scryfall_id,
                    oracle_id=c.oracle_id,
                    name=c.name,
                    set_code=c.set_code,
                    set_name=c.set_name or payload.set_name,
                    collector_number=c.collector_number,
                    rarity=c.rarity,
                    image_small=c.image_small,
                ))
                added += 1

    await db.flush()

    count = (await db.scalar(
        select(func.count())
        .select_from(CollectionEntry)
        .where(CollectionEntry.set_code == payload.set_code)
    )) or 0

    await db.commit()
    return BatchToggleResponse(added=added, removed=removed, set_owned_count=count)


# ── Global collection stats ────────────────────────────────────────────

class CollectionStats(BaseModel):
    total_owned: int = 0
    unique_sets: int = 0


@router.get("/stats", response_model=CollectionStats)
async def collection_stats(db: DbDep) -> CollectionStats:
    total = (await db.scalar(select(func.count()).select_from(CollectionEntry))) or 0
    sets = (await db.scalar(
        select(func.count(func.distinct(CollectionEntry.set_code)))
    )) or 0
    return CollectionStats(total_owned=total, unique_sets=sets)
