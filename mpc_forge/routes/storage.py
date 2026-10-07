from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from mpc_forge.services.system import storage as storage_service

router = APIRouter(prefix="/api/storage", tags=["storage"])


class CategoryUsage(BaseModel):
    key: str
    kind: str
    path: str
    exists: bool
    files: int
    bytes: int
    purge_target: str | None = None
    reclaimable: bool = False


class VolumeUsage(BaseModel):
    mount: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    app_bytes: int
    categories: list[str]


class Totals(BaseModel):
    bytes: int
    files: int
    reclaimable_bytes: int
    by_kind: dict[str, int]


class BackupsSummary(BaseModel):
    count: int
    automatic: int
    manual: int
    bytes: int
    latest_at: str | None = None


class BackupEstimate(BaseModel):
    full_bytes: int
    db_only_bytes: int


class StorageResponse(BaseModel):
    generated_at: str
    cached: bool = False
    age_seconds: float = 0.0
    scan_seconds: float = 0.0
    data_dir: str
    categories: list[CategoryUsage]
    totals: Totals
    volumes: list[VolumeUsage]
    backups: BackupsSummary
    backup_estimate: BackupEstimate


class PurgeRequest(BaseModel):
    targets: list[str] = Field(default_factory=list)
    exports_older_than_days: int = 0
    keep_backups: int = storage_service.DEFAULT_KEEP_BACKUPS


class PurgeResponse(BaseModel):
    targets: dict[str, dict[str, int]]
    removed: int
    freed_bytes: int
    storage: StorageResponse


@router.get("/", response_model=StorageResponse)
async def get_storage(
    refresh: bool = Query(
        False,
        description="Fuerza un escaneo nuevo en vez de usar el snapshot cacheado.",
    ),
) -> Any:
    return await storage_service.snapshot(refresh=refresh)


@router.post("/purge", response_model=PurgeResponse)
async def purge_storage(payload: PurgeRequest) -> Any:
    if not payload.targets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No se ha indicado qué liberar")
    try:
        result = await asyncio.to_thread(
            storage_service.purge,
            payload.targets,
            exports_older_than_days=payload.exports_older_than_days,
            keep_backups=payload.keep_backups,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {**result, "storage": await storage_service.snapshot(refresh=True)}
