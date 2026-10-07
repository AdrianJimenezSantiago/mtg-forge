from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import ArtSource
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.indexing import art_sources, gdrive_indexer
from mpc_forge.services.indexing.source_types import list_registered, resolve
from mpc_forge.services.indexing.source_types.base import ArtSourceTypeError
from mpc_forge.services.indexing.source_types.local_folder import LocalFolderSourceType

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])
local_source_router = APIRouter(tags=["integrations"])


class ArtSourceView(BaseModel):
    id: int
    name: str
    url: str
    source_type: str
    description: str
    tags: list[str]
    pinned: bool
    indexed_at: str | None = None
    indexed_files: int = 0
    index_error: str = ""

    @classmethod
    def from_model(cls, s: ArtSource) -> ArtSourceView:
        return cls(
            id=s.id,
            name=s.name,
            url=s.url,
            source_type=s.source_type,
            description=s.description,
            tags=[t.strip() for t in (s.tags or "").split(",") if t.strip()],
            pinned=s.pinned,
            indexed_at=s.indexed_at.isoformat() if s.indexed_at else None,
            indexed_files=s.indexed_files or 0,
            index_error=s.index_error or "",
        )


class CreateArtSourceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    url: str = Field(..., min_length=1, max_length=512)
    description: str = ""
    tags: str = ""
    pinned: bool = False


class UpdateArtSourceRequest(BaseModel):
    name: str | None = None
    url: str | None = None
    description: str | None = None
    tags: str | None = None
    pinned: bool | None = None


@router.get("/art-sources/", response_model=list[ArtSourceView])
async def list_art_sources(db: DbDep) -> list[ArtSourceView]:
    sources = await art_sources.list_sources(db)
    return [ArtSourceView.from_model(s) for s in sources]


@router.post("/art-sources/", response_model=ArtSourceView, status_code=status.HTTP_201_CREATED)
async def create_art_source(payload: CreateArtSourceRequest, db: DbDep) -> ArtSourceView:
    try:
        src = await art_sources.add_source(
            db,
            name=payload.name,
            url=payload.url,
            description=payload.description,
            tags=payload.tags,
            pinned=payload.pinned,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return ArtSourceView.from_model(src)


@router.patch("/art-sources/{source_id}", response_model=ArtSourceView)
async def update_art_source(
    source_id: int,
    payload: UpdateArtSourceRequest,
    db: DbDep,
) -> ArtSourceView:
    src = await art_sources.update_source(
        db,
        source_id,
        name=payload.name,
        url=payload.url,
        description=payload.description,
        tags=payload.tags,
        pinned=payload.pinned,
    )
    if not src:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source no encontrado")
    return ArtSourceView.from_model(src)


@router.delete("/art-sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_art_source(source_id: int, db: DbDep) -> None:
    ok = await art_sources.delete_source(db, source_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Source no encontrado")


class RestoreCatalogResponse(BaseModel):
    added: int
    skipped: int
    total_curated: int


@router.post("/art-sources/restore-catalog", response_model=RestoreCatalogResponse)
async def restore_catalog(db: DbDep) -> RestoreCatalogResponse:
    result = await art_sources.restore_catalog(db)
    return RestoreCatalogResponse(**result)


class CatalogInfoResponse(BaseModel):
    total_curated: int


@router.get("/art-sources/catalog-info", response_model=CatalogInfoResponse)
async def catalog_info() -> CatalogInfoResponse:
    return CatalogInfoResponse(total_curated=art_sources.catalog_size())


class IndexResponse(BaseModel):
    source_id: int
    files_added: int
    files_updated: int
    folders_visited: int
    used_api_key: bool
    error: str | None = None


async def _run_index_task(source_id: int) -> None:
    try:
        async with session_scope() as db:
            await gdrive_indexer.index_source(db, source_id)
    except Exception as e:
        log.exception("Fallo indexando source %d", source_id)
        try:
            async with session_scope() as db:
                src = await db.get(ArtSource, source_id)
                if src:
                    src.indexed_at = datetime.now(UTC)
                    src.index_error = f"Fallo interno: {type(e).__name__}: {str(e)[:200]}"
        except Exception:
            log.exception("Además no se pudo marcar el error en source %d", source_id)


@router.post("/art-sources/{source_id}/index", response_model=IndexResponse)
async def index_source(
    source_id: int,
    db: DbDep,
    background: BackgroundTasks,
    wait: bool = False,
) -> IndexResponse:
    if wait:
        result = await gdrive_indexer.index_source(db, source_id)
        return IndexResponse(
            source_id=result.source_id,
            files_added=result.files_added,
            files_updated=result.files_updated,
            folders_visited=result.folders_visited,
            used_api_key=result.used_api_key,
            error=result.error,
        )
    background.add_task(_run_index_task, source_id)
    return IndexResponse(
        source_id=source_id,
        files_added=0,
        files_updated=0,
        folders_visited=0,
        used_api_key=bool(cfg.GOOGLE_API_KEY),
        error=None,
    )


@router.delete("/art-sources/{source_id}/index", status_code=status.HTTP_200_OK)
async def clear_index(source_id: int, db: DbDep) -> dict[str, Any]:
    n = await gdrive_indexer.clear_index(db, source_id)
    return {"deleted": n}


class ValidateSourceRequest(BaseModel):
    url: str
    source_type: str | None = None


class ValidateSourceResponse(BaseModel):
    valid: bool
    detected_type: str
    canonical_url: str
    label: str
    error: str | None = None


@router.post("/art-sources/validate", response_model=ValidateSourceResponse)
async def validate_source(payload: ValidateSourceRequest) -> ValidateSourceResponse:
    raw = (payload.url or "").strip()
    if not raw:
        return ValidateSourceResponse(
            valid=False,
            detected_type="",
            canonical_url="",
            label="",
            error="URL vacía",
        )

    if payload.source_type:
        type_cls = resolve(payload.source_type)
        if type_cls is None:
            valid_types = ", ".join(k for k, _ in list_registered())
            return ValidateSourceResponse(
                valid=False,
                detected_type=payload.source_type,
                canonical_url=raw,
                label="",
                error=f"Tipo desconocido: {payload.source_type!r}. Válidos: {valid_types}",
            )
        try:
            canonical = type_cls.validate_url(raw)
        except ValueError as e:
            return ValidateSourceResponse(
                valid=False,
                detected_type=payload.source_type,
                canonical_url=raw,
                label=type_cls.label,
                error=str(e),
            )
        return ValidateSourceResponse(
            valid=True,
            detected_type=payload.source_type,
            canonical_url=canonical,
            label=type_cls.label,
        )

    try:
        detected_type, canonical = art_sources._detect_source_type(raw)
    except Exception as e:
        return ValidateSourceResponse(
            valid=False,
            detected_type="",
            canonical_url=raw,
            label="",
            error=f"Error al detectar tipo: {e}",
        )

    type_cls = resolve(detected_type)
    label = type_cls.label if type_cls else "Otro"
    if detected_type == "other":
        return ValidateSourceResponse(
            valid=False,
            detected_type=detected_type,
            canonical_url=canonical,
            label=label,
            error="No se pudo detectar un tipo conocido. Elige uno manualmente.",
        )
    return ValidateSourceResponse(
        valid=True,
        detected_type=detected_type,
        canonical_url=canonical,
        label=label,
    )


@local_source_router.get("/local-source/{source_id}/{file_id}")
async def serve_local_source_file(
    source_id: int,
    file_id: str,
    db: DbDep,
) -> FileResponse:
    source = await db.get(ArtSource, source_id)
    if not source or source.source_type != "local-folder":
        raise HTTPException(404, "Source no encontrado o no es de tipo local-folder")

    try:
        path = LocalFolderSourceType.resolve_path(source, file_id)
    except ArtSourceTypeError as e:
        raise HTTPException(400, str(e)) from e
    return FileResponse(path)
