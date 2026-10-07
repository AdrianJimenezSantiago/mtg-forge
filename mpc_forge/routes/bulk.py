from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from mpc_forge.db import analyze_table, session_scope
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.cards import bulk_data

router = APIRouter(prefix="/api/bulk", tags=["bulk-data"])
log = logging.getLogger(__name__)


class BulkStatusResponse(BaseModel):
    printings: int = 0
    unique_cards: int = 0
    ijson_available: bool = False
    syncs: list[dict[str, Any]] = []
    progress: dict[str, Any] = {}


class BulkCheckResponse(BaseModel):
    needs_sync: bool
    reason: str


class BulkStartResponse(BaseModel):
    started: bool
    kind: str
    message: str


@router.get("/status", response_model=BulkStatusResponse)
async def status_endpoint(db: DbDep) -> BulkStatusResponse:
    stats = await bulk_data.local_stats(db)
    return BulkStatusResponse(**stats, progress=bulk_data.get_progress())


@router.get("/check", response_model=BulkCheckResponse)
async def check_endpoint(db: DbDep, kind: str = bulk_data.DEFAULT_KIND) -> BulkCheckResponse:
    if kind not in bulk_data.BULK_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Volcado desconocido: {kind}")
    needed, reason = await bulk_data.needs_sync(db, kind)
    return BulkCheckResponse(needs_sync=needed, reason=reason)


@router.post("/sync", response_model=BulkStartResponse)
async def start_sync(kind: str = bulk_data.DEFAULT_KIND, force: bool = False) -> BulkStartResponse:
    if kind not in bulk_data.BULK_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Volcado desconocido: {kind}")

    current = bulk_data.get_progress()
    if current.get("active"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Ya hay una importación en curso ({current['kind']}, {current['percent']}%)",
        )

    async def _with_analyze() -> None:
        async with session_scope() as session:
            await bulk_data.sync(session, kind, force=force)
        await analyze_table("printings")

    task = asyncio.create_task(_with_analyze(), name=f"bulk-sync-{kind}")
    bulk_data._progress._task = task

    return BulkStartResponse(
        started=True,
        kind=kind,
        message="Importación arrancada. Consulta /api/bulk/status para el avance.",
    )


@router.post("/cancel")
async def cancel_sync() -> dict[str, bool]:
    return {"cancelled": await bulk_data.cancel()}


@router.get("/progress")
async def progress_endpoint() -> dict[str, Any]:
    return bulk_data.get_progress()
