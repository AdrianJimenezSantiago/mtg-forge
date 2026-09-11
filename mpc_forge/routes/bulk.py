"""Endpoints del modo offline (importación del bulk data de Scryfall).

Se separan de ``integrations.py``, que ya son 1.100 líneas, porque este es un
subsistema con su propio ciclo de vida: manifiesto, descarga larga, progreso,
cancelación y estado persistido.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.db import analyze_table, get_session, session_scope
from mpc_forge.services import bulk_data

router = APIRouter(prefix="/api/bulk", tags=["bulk-data"])
log = logging.getLogger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_session)]


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
    """Cuántas impresiones hay en local y estado de la importación en curso."""
    stats = await bulk_data.local_stats(db)
    return BulkStatusResponse(**stats, progress=bulk_data.get_progress())


@router.get("/check", response_model=BulkCheckResponse)
async def check_endpoint(
    db: DbDep, kind: str = bulk_data.DEFAULT_KIND
) -> BulkCheckResponse:
    """Consulta a Scryfall si hay un volcado más reciente que el importado."""
    if kind not in bulk_data.BULK_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Volcado desconocido: {kind}")
    needed, reason = await bulk_data.needs_sync(db, kind)
    return BulkCheckResponse(needs_sync=needed, reason=reason)


@router.post("/sync", response_model=BulkStartResponse)
async def start_sync(
    kind: str = bulk_data.DEFAULT_KIND, force: bool = False
) -> BulkStartResponse:
    """Arranca la importación en segundo plano.

    Devuelve inmediatamente: la descarga tarda varios minutos y el cliente
    sigue el avance con ``GET /api/bulk/status``. Se rechaza si ya hay una
    importación viva — dos descargas simultáneas saturarían la red y
    multiplicarían la contención de escritura en SQLite.
    """
    if kind not in bulk_data.BULK_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Volcado desconocido: {kind}")

    current = bulk_data.get_progress()
    if current.get("active"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Ya hay una importación en curso ({current['kind']}, "
            f"{current['percent']}%)",
        )

    async def _with_analyze() -> None:
        async with session_scope() as session:
            await bulk_data.sync(session, kind, force=force)
        # Tras insertar cientos de miles de filas, el planificador de consultas
        # sigue usando las estadísticas de la tabla pequeña y puede ignorar los
        # índices. ANALYZE lo corrige.
        await analyze_table("printings")

    import asyncio
    task = asyncio.create_task(_with_analyze(), name=f"bulk-sync-{kind}")
    bulk_data._progress._task = task

    return BulkStartResponse(
        started=True,
        kind=kind,
        message="Importación arrancada. Consulta /api/bulk/status para el avance.",
    )


@router.post("/cancel")
async def cancel_sync() -> dict[str, bool]:
    """Cancela la importación en curso.

    Lo ya importado se conserva: los lotes se confirman según llegan, así que
    cancelar deja una base parcial perfectamente usable, no un estado corrupto.
    """
    return {"cancelled": await bulk_data.cancel()}


@router.get("/progress")
async def progress_endpoint() -> dict[str, Any]:
    """Progreso para el polling del frontend."""
    return bulk_data.get_progress()
