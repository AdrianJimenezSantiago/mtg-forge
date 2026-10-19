from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, bindparam, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import CollectionEntry, KeyValue, PrintingCache
from mpc_forge.services.cards import printings
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)

# Scryfall publica en /migrations las cartas que fusiona con otra o elimina
# (sobre todo versiones de Arena retiradas). Se aplican a los datos vivos del
# usuario; el historial (tiradas, actividad, snapshots) se deja como se hizo.
_MERGE_TARGETS = (
    ("deck_cards", "scryfall_id"),
    ("decks", "commander_scryfall_id"),
    ("art_preferences", "scryfall_id"),
    ("physical_inventory", "scryfall_id"),
    ("art_theme_entries", "scryfall_id"),
)

_CHECKED_KEY = "scryfall_migrations.checked_at"
_SINCE_KEY = "scryfall_migrations.since"

CHECK_EVERY = timedelta(days=1)


async def _kv(db: AsyncSession, key: str) -> str | None:
    row = await db.get(KeyValue, key)
    return row.value if row else None


async def _set_kv(db: AsyncSession, key: str, value: str) -> None:
    row = await db.get(KeyValue, key)
    if row is None:
        db.add(KeyValue(key=key, value=value))
    else:
        row.value = value


async def _referenced(db: AsyncSession, ids: list[str]) -> set[str]:
    found: set[str] = set()
    for table, column in _MERGE_TARGETS:
        for chunk in chunked(ids):
            stmt = text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IN :ids")
            rows = await db.execute(
                stmt.bindparams(bindparam("ids", expanding=True)), {"ids": chunk}
            )
            found.update(r[0] for r in rows)
    return found


async def apply(db: AsyncSession, migrations: list[dict[str, Any]]) -> dict[str, int]:
    merges = {
        m["old_scryfall_id"]: m["new_scryfall_id"]
        for m in migrations
        if m.get("migration_strategy") == "merge"
        and m.get("old_scryfall_id")
        and m.get("new_scryfall_id")
    }
    deletes = {
        m["old_scryfall_id"]
        for m in migrations
        if m.get("migration_strategy") == "delete" and m.get("old_scryfall_id")
    }
    stats = {"merged": 0, "collection": 0, "printings_removed": 0}

    if merges:
        params = [{"old": old, "new": new} for old, new in merges.items()]
        for table, column in _MERGE_TARGETS:
            result = await db.execute(
                text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"), params
            )
            stats["merged"] += cast(CursorResult[Any], result).rowcount or 0

        olds = list(merges)
        for chunk in chunked(olds):
            entries = (
                await db.scalars(
                    select(CollectionEntry).where(CollectionEntry.scryfall_id.in_(chunk))
                )
            ).all()
            for entry in entries:
                new_id = merges[entry.scryfall_id]
                if await db.get(CollectionEntry, new_id) is None:
                    await db.execute(
                        update(CollectionEntry)
                        .where(CollectionEntry.scryfall_id == entry.scryfall_id)
                        .values(scryfall_id=new_id)
                    )
                else:
                    await db.delete(entry)
                stats["collection"] += 1

    # Una impresión fusionada ya no la usa nadie; una eliminada se conserva
    # mientras algún mazo la tenga para que siga mostrándose.
    stale = list(merges) + sorted(deletes - await _referenced(db, list(deletes)))
    for chunk in chunked(stale):
        result = await db.execute(delete(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        stats["printings_removed"] += cast(CursorResult[Any], result).rowcount or 0

    await db.commit()
    return stats


async def _cache_new_printings(
    db: AsyncSession, scryfall: ScryfallClient, new_ids: list[str]
) -> None:
    referenced = await _referenced(db, new_ids)
    missing: list[str] = []
    for chunk in chunked(list(referenced)):
        have = set(
            (
                await db.scalars(
                    select(PrintingCache.scryfall_id).where(PrintingCache.scryfall_id.in_(chunk))
                )
            ).all()
        )
        missing.extend(i for i in chunk if i not in have)
    if missing:
        await printings.upsert_printings(
            db, await scryfall.collection([{"id": i} for i in missing])
        )
        await db.commit()


async def sync_if_stale(db: AsyncSession, scryfall: ScryfallClient) -> dict[str, Any]:
    checked = await _kv(db, _CHECKED_KEY)
    now = datetime.now(UTC)
    if checked and now - datetime.fromisoformat(checked) < CHECK_EVERY:
        return {"synced": False, "reason": "up_to_date"}

    since = await _kv(db, _SINCE_KEY)
    migrations = await scryfall.migrations(since)
    stats = await apply(db, migrations)
    new_ids = [m["new_scryfall_id"] for m in migrations if m.get("new_scryfall_id")]
    if new_ids:
        await _cache_new_printings(db, scryfall, new_ids)

    latest = max((m.get("performed_at") or "" for m in migrations), default="")
    if latest:
        await _set_kv(db, _SINCE_KEY, max(latest, since or ""))
    await _set_kv(db, _CHECKED_KEY, now.isoformat())
    await db.commit()
    if stats["merged"] or stats["collection"] or stats["printings_removed"]:
        log.info("Migraciones de Scryfall aplicadas (%d revisadas): %s", len(migrations), stats)
    return {"synced": True, "migrations": len(migrations), **stats}
