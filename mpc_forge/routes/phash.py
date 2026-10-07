from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.art import phash
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])

PHASH_UNAVAILABLE = "pHash no disponible — instala `pip install Pillow imagehash`"


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=15.0,
        verify=not ssl_insecure(),
        headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
    )


def _require_phash() -> None:
    if not phash.is_available():
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, PHASH_UNAVAILABLE)


class PHashStatsResponse(BaseModel):
    available: bool
    enabled: bool
    total_arts: int
    with_hash: int
    coverage_pct: float


class PHashComputeRequest(BaseModel):
    source_id: int
    limit: int = Field(default=500, ge=1, le=5000)


class PHashComputeResponse(BaseModel):
    computed: int
    failed: int
    skipped: int
    error: str | None = None


class SimilarArtsResponse(BaseModel):
    reference_file_id: str
    reference_hash: str | None
    similar: list[dict[str, Any]]


@router.get("/drives/phash/stats", response_model=PHashStatsResponse)
async def phash_stats(db: DbDep) -> PHashStatsResponse:
    total = int(await db.scalar(select(func.count(IndexedArt.id))) or 0)
    with_hash = int(
        await db.scalar(select(func.count(IndexedArt.id)).where(IndexedArt.image_hash.is_not(None)))
        or 0
    )
    return PHashStatsResponse(
        available=phash.is_available(),
        enabled=await phash.enabled(db),
        total_arts=total,
        with_hash=with_hash,
        coverage_pct=(100.0 * with_hash / total) if total else 0.0,
    )


async def _phash_compute_task(source_id: int, limit: int) -> None:
    try:
        async with (
            session_scope() as db,
            _http_client() as client,
        ):
            stats = await phash.compute_missing_for_source(
                db,
                client,
                source_id=source_id,
                limit=limit,
            )
            log.info("pHash retrofit source=%d: %s", source_id, stats)
    except Exception:
        log.exception("phash_compute_task falló para source %d", source_id)


@router.post("/drives/phash/compute", response_model=PHashComputeResponse)
async def phash_compute(
    payload: PHashComputeRequest,
    db: DbDep,
    background: BackgroundTasks,
    wait: bool = False,
) -> PHashComputeResponse:
    _require_phash()

    if not wait:
        background.add_task(_phash_compute_task, payload.source_id, payload.limit)
        return PHashComputeResponse(
            computed=0, failed=0, skipped=0, error="scheduled in background"
        )

    async with _http_client() as client:
        stats = await phash.compute_missing_for_source(
            db,
            client,
            source_id=payload.source_id,
            limit=payload.limit,
        )
    return PHashComputeResponse(**stats)


class PHashComputeAllRequest(BaseModel):
    limit_per_source: int = Field(default=200, ge=1, le=5000)


async def _phash_compute_all_task(limit_per_source: int) -> None:
    try:
        async with session_scope() as db:
            sources = (await db.scalars(select(ArtSource))).all()
        log.info("pHash retrofit-all: %d sources a procesar", len(sources))

        async with _http_client() as client:
            for src in sources:
                try:
                    async with session_scope() as db:
                        stats = await phash.compute_missing_for_source(
                            db,
                            client,
                            source_id=src.id,
                            limit=limit_per_source,
                        )
                        log.info("pHash retrofit source=%d (%s): %s", src.id, src.name, stats)
                except Exception:
                    log.exception("pHash retrofit falló para source %d", src.id)
                    continue
    except Exception:
        log.exception("_phash_compute_all_task falló")


@router.post("/drives/phash/compute-all", response_model=dict[str, Any])
async def phash_compute_all(
    payload: PHashComputeAllRequest,
    background: BackgroundTasks,
) -> dict[str, Any]:
    _require_phash()
    background.add_task(_phash_compute_all_task, payload.limit_per_source)
    return {
        "status": "scheduled",
        "message": "Job iniciado en background. Consulta /drives/phash/stats para ver progreso.",
    }


@router.get("/drives/phash/similar/{file_id}", response_model=SimilarArtsResponse)
async def phash_similar(
    file_id: str,
    db: DbDep,
    threshold: int = 8,
    limit: int = 50,
) -> SimilarArtsResponse:
    reference = await db.scalar(select(IndexedArt).where(IndexedArt.file_id == file_id))
    if not reference or not reference.image_hash:
        return SimilarArtsResponse(
            reference_file_id=file_id,
            reference_hash=None,
            similar=[],
        )
    similars = await phash.find_similar(
        db,
        reference.image_hash,
        threshold=max(0, min(32, threshold)),
        exclude_file_id=file_id,
        limit=limit,
    )
    return SimilarArtsResponse(
        reference_file_id=file_id,
        reference_hash=reference.image_hash,
        similar=[
            {
                "file_id": a.file_id,
                "filename": a.filename,
                "source_id": a.source_id,
                "folder_path": a.folder_path,
                "image_hash": a.image_hash,
                "hamming": phash.hamming_distance(reference.image_hash, a.image_hash or ""),
            }
            for a in similars
        ],
    )
