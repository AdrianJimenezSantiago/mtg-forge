"""Endpoints de integraciones:
  - MPC Autofill desktop tool (status/launch)
  - Gestión manual de art sources (Google Drives comunitarios)
  - Indexado + búsqueda fuzzy en drives
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session, session_scope
from mpc_forge.services import art_sources, dfc_pairs, gdrive_indexer, gdrive_search, mpc_autofill

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


# ============================================================================
# MPC Autofill desktop tool (chilli-axe)
# ============================================================================

class AutofillStatusResponse(BaseModel):
    available: bool
    exe_path: str | None = None
    source: str
    hint: str | None = None


class AutofillLaunchRequest(BaseModel):
    xml_filename: str


@router.get("/mpc-autofill/status", response_model=AutofillStatusResponse)
async def autofill_status() -> AutofillStatusResponse:
    st = mpc_autofill.detect()
    hint = None
    if not st.available:
        hint = (
            "Descarga el binario desde github.com/chilli-axe/mpc-autofill/releases "
            "y colócalo en la carpeta del proyecto (o configura su ruta en Ajustes)."
        )
    return AutofillStatusResponse(
        available=st.available, exe_path=st.exe_path,
        source=st.source, hint=hint,
    )


@router.post("/mpc-autofill/launch")
async def autofill_launch(payload: AutofillLaunchRequest) -> dict[str, Any]:
    exports_dir = Path(cfg.PATHS.exports_dir).resolve()
    xml_path = (exports_dir / payload.xml_filename).resolve()

    try:
        xml_path.relative_to(exports_dir)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida")

    if not xml_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"XML no encontrado: {payload.xml_filename}")

    try:
        pid = mpc_autofill.launch(xml_path)
    except RuntimeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"launched": True, "pid": pid, "xml_path": str(xml_path)}


# ============================================================================
# Art Sources (gestión manual de drives comunitarios)
# ============================================================================

class ArtSourceView(BaseModel):
    id: int
    name: str
    url: str
    source_type: str
    description: str
    tags: list[str]
    pinned: bool
    indexed_at: str | None = None       # ISO8601 o None
    indexed_files: int = 0
    index_error: str = ""

    @classmethod
    def from_model(cls, s) -> "ArtSourceView":
        return cls(
            id=s.id, name=s.name, url=s.url, source_type=s.source_type,
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
    """Devuelve la lista completa de sources del usuario."""
    sources = await art_sources.list_sources(db)
    return [ArtSourceView.from_model(s) for s in sources]


@router.post("/art-sources/", response_model=ArtSourceView, status_code=status.HTTP_201_CREATED)
async def create_art_source(payload: CreateArtSourceRequest, db: DbDep) -> ArtSourceView:
    try:
        src = await art_sources.add_source(
            db, name=payload.name, url=payload.url,
            description=payload.description, tags=payload.tags,
            pinned=payload.pinned,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return ArtSourceView.from_model(src)


@router.patch("/art-sources/{source_id}", response_model=ArtSourceView)
async def update_art_source(
    source_id: int, payload: UpdateArtSourceRequest, db: DbDep,
) -> ArtSourceView:
    src = await art_sources.update_source(
        db, source_id,
        name=payload.name, url=payload.url,
        description=payload.description, tags=payload.tags,
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
    """Añade al catálogo cualquier drive del catálogo curado (los 67 de MPCFill)
    que el usuario haya borrado. No toca los que ya existen. Idempotente.
    """
    result = await art_sources.restore_catalog(db)
    return RestoreCatalogResponse(**result)


class CatalogInfoResponse(BaseModel):
    total_curated: int


@router.get("/art-sources/catalog-info", response_model=CatalogInfoResponse)
async def catalog_info() -> CatalogInfoResponse:
    return CatalogInfoResponse(total_curated=art_sources.catalog_size())


# ============================================================================
# Indexado + búsqueda fuzzy de arte en Google Drives
# ============================================================================

class IndexResponse(BaseModel):
    source_id: int
    files_added: int
    files_updated: int
    folders_visited: int
    used_api_key: bool
    error: str | None = None


async def _run_index_task(source_id: int) -> None:
    """Se ejecuta en background para no bloquear la request HTTP.

    Si el indexado falla por lo que sea, garantiza que el source queda marcado
    con `index_error` para que el frontend deje de creer que sigue indexando.
    """
    try:
        async with session_scope() as db:
            await gdrive_indexer.index_source(db, source_id)
    except Exception as e:  # noqa: BLE001
        log.exception("Fallo indexando source %d", source_id)
        # Segundo intento: marcar el source con el error en su propia sesión.
        try:
            from datetime import datetime, timezone
            from mpc_forge.models import ArtSource
            async with session_scope() as db:
                src = await db.get(ArtSource, source_id)
                if src:
                    src.indexed_at = datetime.now(timezone.utc)
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
    """Indexa un drive. Por defecto lanza en background y devuelve 202-like.

    Con `?wait=true` bloquea hasta que termine (útil para drives pequeños).
    """
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
    # Async: lanzamos y devolvemos "iniciado"
    background.add_task(_run_index_task, source_id)
    return IndexResponse(
        source_id=source_id, files_added=0, files_updated=0,
        folders_visited=0, used_api_key=bool(cfg.GOOGLE_API_KEY),
        error=None,
    )


@router.delete("/art-sources/{source_id}/index", status_code=status.HTTP_200_OK)
async def clear_index(source_id: int, db: DbDep) -> dict:
    """Borra el índice de un drive (mantiene el ArtSource, solo vacía IndexedArt)."""
    n = await gdrive_indexer.clear_index(db, source_id)
    return {"deleted": n}


class SearchHit(BaseModel):
    file_id: str
    filename: str
    source_id: int
    source_name: str
    folder_path: str
    thumb_url: str
    download_url: str
    score: int
    tags: list[str] = []
    is_full_art: bool = False
    is_borderless: bool = False
    is_extended: bool = False
    is_showcase: bool = False
    is_retro: bool = False
    is_textless: bool = False
    is_promo: bool = False
    is_alt_art: bool = False


def _parse_csv_list(raw: str | None) -> list[str] | None:
    """Convierte 'a,b, c' → ['a', 'b', 'c']. Devuelve None si vacío o None."""
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


@router.get("/drives/search", response_model=list[SearchHit])
async def drives_search(
    q: str,
    db: DbDep,
    limit: int = 20,
    source_id: int | None = None,
    tags_include: str | None = None,
    tags_exclude: str | None = None,
) -> list[SearchHit]:
    """Busca `q` (nombre de carta) en el índice de todos los drives.

    Filtros opcionales:
      - ``tags_include=full_art,borderless``: solo artes que TENGAN todos.
      - ``tags_exclude=promo``: descarta artes con cualquiera de los indicados.

    Devuelve top-N resultados con thumbnails y URLs de descarga listas.
    """
    if not q.strip():
        return []
    source_ids = [source_id] if source_id else None
    results = await gdrive_search.search(
        db, q, limit=limit,
        source_ids=source_ids,
        tags_include=_parse_csv_list(tags_include),
        tags_exclude=_parse_csv_list(tags_exclude),
    )
    return [
        SearchHit(
            file_id=r.file_id, filename=r.filename,
            source_id=r.source_id, source_name=r.source_name,
            folder_path=r.folder_path,
            thumb_url=r.thumb_url, download_url=r.download_url,
            score=r.score,
            tags=r.tags,
            is_full_art=r.is_full_art,
            is_borderless=r.is_borderless,
            is_extended=r.is_extended,
            is_showcase=r.is_showcase,
            is_retro=r.is_retro,
            is_textless=r.is_textless,
            is_promo=r.is_promo,
            is_alt_art=r.is_alt_art,
        )
        for r in results
    ]


class DriveStatsResponse(BaseModel):
    total_files: int
    sources_indexed: int


@router.get("/drives/stats", response_model=DriveStatsResponse)
async def drives_stats(db: DbDep) -> DriveStatsResponse:
    s = await gdrive_search.stats(db)
    return DriveStatsResponse(**s)


# ============================================================================
# DFC pairs cache (precomputado desde Scryfall bulk data)
# ============================================================================

def _get_scryfall(request) -> ScryfallClient:
    return request.app.state.scryfall


class DFCPairsStatsResponse(BaseModel):
    total_pairs: int
    last_synced_at: str | None = None


class DFCPairsSyncResponse(BaseModel):
    synced: bool
    pairs: int
    reason: str


@router.get("/dfc-pairs/stats", response_model=DFCPairsStatsResponse)
async def dfc_pairs_stats(db: DbDep) -> DFCPairsStatsResponse:
    """Estado actual del cache: cuántos pares tenemos y cuándo se sincronizó."""
    return DFCPairsStatsResponse(**await dfc_pairs.stats(db))


async def _run_dfc_sync_task(scryfall: ScryfallClient) -> None:
    """Ejecuta el sync en background con su propia sesión de BD."""
    try:
        async with session_scope() as db:
            await dfc_pairs.sync_if_stale(db, scryfall)
    except Exception:  # noqa: BLE001
        log.exception("Sync manual de DFC pairs falló")


@router.post("/dfc-pairs/sync", response_model=DFCPairsSyncResponse)
async def dfc_pairs_sync(
    db: DbDep,
    background: BackgroundTasks,
    request: __import__("fastapi").Request,
    force: bool = False,
    wait: bool = True,
) -> DFCPairsSyncResponse:
    """Fuerza (o pide) una sincronización con Scryfall.

    - ``force=false`` (default): solo sincroniza si TTL expirado.
    - ``force=true``: fuerza el refresh aunque acabemos de sincronizar.
    - ``wait=true`` (default): bloquea hasta que termine y devuelve el resultado.
    - ``wait=false``: lanza en background y responde inmediatamente.
    """
    scryfall = _get_scryfall(request)

    if force:
        # Truco: marcamos como stale borrando el timestamp, para que
        # sync_if_stale considere que hay que refrescar.
        from mpc_forge.models import KeyValue
        kv = await db.get(KeyValue, "dfc_pairs.last_synced_at")
        if kv:
            await db.delete(kv)
            await db.commit()

    if not wait:
        background.add_task(_run_dfc_sync_task, scryfall)
        current = await dfc_pairs.stats(db)
        return DFCPairsSyncResponse(
            synced=False, pairs=current["total_pairs"], reason="scheduled",
        )

    result = await dfc_pairs.sync_if_stale(db, scryfall)
    return DFCPairsSyncResponse(
        synced=bool(result.get("synced", False)),
        pairs=int(result.get("pairs", 0)),
        reason=str(result.get("reason", "")),
    )
