from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import session_scope
from mpc_forge.models import KeyValue
from mpc_forge.routes.dependencies import DbDep, ScryfallDep
from mpc_forge.services.cards import dfc_pairs

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])


class DFCPairsStatsResponse(BaseModel):
    total_pairs: int
    last_synced_at: str | None = None


class DFCPairsSyncResponse(BaseModel):
    synced: bool
    pairs: int
    reason: str


@router.get("/dfc-pairs/stats", response_model=DFCPairsStatsResponse)
async def dfc_pairs_stats(db: DbDep) -> DFCPairsStatsResponse:
    return DFCPairsStatsResponse(**await dfc_pairs.stats(db))


class DFCPairsLookupResponse(BaseModel):
    found: dict[str, dict[str, str]]
    total_queried: int
    total_matched: int


@router.get("/dfc-pairs/lookup", response_model=DFCPairsLookupResponse)
async def dfc_pairs_lookup(
    db: DbDep,
    names: str = "",
) -> DFCPairsLookupResponse:
    parts = [n.strip() for n in names.split("|") if n.strip()]
    found = await dfc_pairs.bulk_lookup(db, parts)
    return DFCPairsLookupResponse(
        found=found,
        total_queried=len(parts),
        total_matched=len(found),
    )


async def _run_dfc_sync_task(scryfall: ScryfallClient) -> None:
    try:
        async with session_scope() as db:
            await dfc_pairs.sync_if_stale(db, scryfall)
    except Exception:
        log.exception("Sync manual de DFC pairs falló")


@router.post("/dfc-pairs/sync", response_model=DFCPairsSyncResponse)
async def dfc_pairs_sync(
    db: DbDep,
    background: BackgroundTasks,
    scryfall: ScryfallDep,
    force: bool = False,
    wait: bool = True,
) -> DFCPairsSyncResponse:
    if force:
        kv = await db.get(KeyValue, "dfc_pairs.last_synced_at")
        if kv:
            await db.delete(kv)
            await db.commit()

    if not wait:
        background.add_task(_run_dfc_sync_task, scryfall)
        current = await dfc_pairs.stats(db)
        return DFCPairsSyncResponse(
            synced=False,
            pairs=current["total_pairs"],
            reason="scheduled",
        )

    result = await dfc_pairs.sync_if_stale(db, scryfall)
    return DFCPairsSyncResponse(
        synced=bool(result.get("synced", False)),
        pairs=int(result.get("pairs", 0)),
        reason=str(result.get("reason", "")),
    )
