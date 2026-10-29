from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel

from mpc_forge.errors import ApiError
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.cards import bulk_data

router = APIRouter(prefix="/api/bulk", tags=["bulk-data"])
log = logging.getLogger(__name__)


class BulkStatusResponse(BaseModel):
    printings: int = 0
    unique_cards: int = 0
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
        raise ApiError(status.HTTP_400_BAD_REQUEST, "unknown_bulk_kind", kind=kind)
    needed, reason = await bulk_data.needs_sync(db, kind)
    return BulkCheckResponse(needs_sync=needed, reason=reason)


@router.post("/sync", response_model=BulkStartResponse)
async def start_sync(kind: str = bulk_data.DEFAULT_KIND, force: bool = False) -> BulkStartResponse:
    if kind not in bulk_data.BULK_KINDS:
        raise ApiError(status.HTTP_400_BAD_REQUEST, "unknown_bulk_kind", kind=kind)

    if bulk_data.is_running():
        current = bulk_data.get_progress()
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "bulk_in_progress",
            kind=current["kind"],
            percent=current["percent"],
        )
    bulk_data.start(kind, force=force)

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
