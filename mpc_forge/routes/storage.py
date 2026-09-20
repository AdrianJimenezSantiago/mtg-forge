"""Endpoints de almacenamiento: cuánto ocupa la app y qué se puede liberar.

El cálculo vive en ``services/storage.py``; aquí solo está el contrato HTTP.
La respuesta se tipa con Pydantic (y no se devuelve el dict a pelo) porque la
vista de Ajustes la consume campo a campo y el esquema de OpenAPI es lo que
avisa si un renombrado rompe al cliente.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from mpc_forge.services import storage as storage_service

router = APIRouter(prefix="/api/storage", tags=["storage"])


class CategoryUsage(BaseModel):
    key: str
    """Identificador estable de la categoría (``art``, ``backups``, …).

    La etiqueta visible NO viaja aquí: la pone el frontend desde el registro
    de traducciones, con la clave ``storage_cat_<key>``.
    """
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
    """Qué liberar: ``thumbs``, ``exports``, ``logs``, ``backups``.

    Cualquier otro valor se rechaza con 400. La lista blanca está en el
    servicio, no aquí, para que no haya dos versiones de la misma regla.
    """
    exports_older_than_days: int = 0
    """``0`` borra todos los exports; ``30`` solo los de más de un mes."""
    keep_backups: int = storage_service.DEFAULT_KEEP_BACKUPS


class PurgeResponse(BaseModel):
    targets: dict[str, dict[str, int]]
    removed: int
    freed_bytes: int
    storage: StorageResponse
    """Desglose recalculado tras la limpieza.

    Va en la misma respuesta para que la interfaz no tenga que encadenar un
    segundo GET y enseñe el resultado sin un parpadeo de cifras viejas.
    """


@router.get("/", response_model=StorageResponse)
async def get_storage(
    refresh: bool = Query(
        False,
        description="Fuerza un escaneo nuevo en vez de usar el snapshot cacheado.",
    ),
) -> Any:
    """Desglose de lo que ocupa en disco todo el contenido local de la app."""
    return await storage_service.snapshot(refresh=refresh)


@router.post("/purge", response_model=PurgeResponse)
async def purge_storage(payload: PurgeRequest) -> Any:
    """Libera espacio de las categorías recuperables.

    Nunca toca la base de datos, el arte custom ni los reversos: son los
    únicos datos que no se pueden volver a generar ni descargar.
    """
    if not payload.targets:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No se ha indicado qué liberar"
        )
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
