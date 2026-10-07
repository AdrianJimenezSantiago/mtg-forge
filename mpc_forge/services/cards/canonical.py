from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import IndexedArt, PrintingCache
from mpc_forge.services.cards.printings import upsert_printings

log = logging.getLogger(__name__)

_SCRYFALL_BATCH_SIZE = 75


async def validate_and_enrich(
    db: AsyncSession,
    scryfall: ScryfallClient,
    limit: int = 500,
    source_id: int | None = None,
) -> dict[str, int]:
    stmt = (
        select(IndexedArt)
        .where(IndexedArt.expansion_code.is_not(None))
        .where(IndexedArt.collector_number.is_not(None))
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(IndexedArt.source_id == source_id)
    arts = (await db.scalars(stmt)).all()
    if not arts:
        return {"checked": 0, "valid": 0, "invalid": 0, "cache_hits": 0}

    by_key: dict[tuple[str, str], list[IndexedArt]] = {}
    for a in arts:
        key = (a.expansion_code, a.collector_number)
        by_key.setdefault(key, []).append(a)

    resolved: dict[tuple[str, str], PrintingCache] = {}
    keys = list(by_key.keys())
    if keys:
        cache_rows = (
            await db.scalars(
                select(PrintingCache).where(
                    tuple_(PrintingCache.set_code, PrintingCache.collector_number).in_(keys)
                )
            )
        ).all()
        for pc in cache_rows:
            k = (pc.set_code, pc.collector_number)
            if k in by_key and k not in resolved:
                resolved[k] = pc
    cache_hits = len(resolved)
    to_query: list[tuple[str, str]] = [k for k in keys if k not in resolved]

    valid = 0
    invalid = 0
    for i in range(0, len(to_query), _SCRYFALL_BATCH_SIZE):
        batch = to_query[i : i + _SCRYFALL_BATCH_SIZE]
        idents = [{"set": s, "collector_number": n} for s, n in batch]
        try:
            cards = await scryfall.collection(idents)
        except Exception as e:
            log.warning("Scryfall.collection batch falló, saltando: %s", e)
            continue

        cached = await upsert_printings(db, cards)
        found_keys: set[tuple[str, str]] = set()
        for c in cards:
            key = (
                (c.get("set") or "").lower(),
                c.get("collector_number") or "",
            )
            found_keys.add(key)
            resolved[key] = cached[c["id"]]
            valid += 1
        for key in batch:
            if key not in found_keys:
                invalid += 1
                for art in by_key.get(key, []):
                    art.expansion_code = None
                    art.collector_number = None
                    art.canonical_source = "invalid"
    await db.commit()

    valid += cache_hits

    return {
        "checked": len(arts),
        "valid": valid,
        "invalid": invalid,
        "cache_hits": cache_hits,
    }


async def get_canonical_details(
    db: AsyncSession, expansion_code: str, collector_number: str
) -> dict[str, Any] | None:
    pc = await db.scalar(
        select(PrintingCache)
        .where(PrintingCache.set_code == expansion_code.lower())
        .where(PrintingCache.collector_number == collector_number)
        .limit(1)
    )
    if not pc:
        return None
    return {
        "scryfall_id": pc.scryfall_id,
        "oracle_id": pc.oracle_id,
        "name": pc.name,
        "artist": pc.artist,
        "set_code": pc.set_code,
        "set_name": pc.set_name,
        "collector_number": pc.collector_number,
        "released_at": pc.released_at,
    }
