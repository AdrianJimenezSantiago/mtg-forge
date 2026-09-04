"""Endpoints de integraciones:
  - MPC Autofill desktop tool (status/launch)
  - Gestión manual de art sources (Google Drives comunitarios)
  - Indexado + búsqueda fuzzy en drives
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
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
    # Metadatos canónicos [SET NUM] (Fase 2 · T5)
    expansion_code: str | None = None
    collector_number: str | None = None
    # Extras · F2/T8: perceptual hash para dedupe cross-drive y "similares".
    image_hash: str | None = None


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
    expansion_code: str | None = None,
) -> list[SearchHit]:
    """Busca `q` (nombre de carta) en el índice de todos los drives.

    Filtros opcionales:
      - ``tags_include=full_art,borderless``: solo artes que TENGAN todos.
      - ``tags_exclude=promo``: descarta artes con cualquiera de los indicados.
      - ``expansion_code=dmu``: solo artes con tag `[DMU NUM]`.

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
        expansion_code=(expansion_code or None),
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
            expansion_code=r.expansion_code,
            collector_number=r.collector_number,
            image_hash=getattr(r, "image_hash", None),
        )
        for r in results
    ]


class DriveStatsResponse(BaseModel):
    total_files: int
    sources_indexed: int
    fts5_available: bool = False


@router.get("/drives/stats", response_model=DriveStatsResponse)
async def drives_stats(db: DbDep) -> DriveStatsResponse:
    s = await gdrive_search.stats(db)
    return DriveStatsResponse(**s)


# ============================================================================
# Servir archivos de sources tipo local-folder (Fase 2 · T7)
# ============================================================================

# El router usa prefix="/api" por defecto; pero queremos /local-source/... sin
# el prefijo (más natural como URL de imagen). Usamos un sub-router propio
# aquí para evitar el prefijo. Se incluye en app.py junto al principal.
_local_source_router = APIRouter(tags=["integrations"])


@_local_source_router.get("/local-source/{source_id}/{file_id}")
async def serve_local_source_file(
    source_id: int,
    file_id: str,
    db: DbDep,
):
    """Sirve un archivo de un source tipo ``local-folder``.

    Seguridad: la resolución del `file_id` en la ruta absoluta va vía
    ``LocalFolderSourceType.resolve_path()`` que valida que la ruta esté
    DENTRO de la carpeta del source (defensa contra path traversal). Cualquier
    intento con ``..`` codificado o similar devuelve 400.

    Este endpoint es la contrapartida del ``download_url()`` /
    ``thumbnail_url()`` que devuelve `LocalFolderSourceType`. Todos los
    thumbnails de artes de un local-folder se sirven desde aquí.
    """
    from fastapi.responses import FileResponse
    from mpc_forge.models import ArtSource
    from mpc_forge.services.source_types import resolve
    from mpc_forge.services.source_types.base import ArtSourceTypeError

    source = await db.get(ArtSource, source_id)
    if not source or source.source_type != "local-folder":
        raise HTTPException(404, "Source no encontrado o no es de tipo local-folder")

    cls = resolve("local-folder")
    if cls is None:
        raise HTTPException(500, "LocalFolderSourceType no disponible")

    try:
        path = cls.resolve_path(source, file_id)  # type: ignore[attr-defined]
    except ArtSourceTypeError as e:
        raise HTTPException(400, str(e))
    return FileResponse(path)


# ============================================================================
# DFC pairs cache (precomputado desde Scryfall bulk data)
# ============================================================================

def _get_scryfall(request: Request) -> ScryfallClient:
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


class DFCPairsLookupResponse(BaseModel):
    """Mapa {nombre_original: {back_name, kind}} de las cartas encontradas
    en el cache. Las cartas no encontradas se omiten del dict (no significa
    que no sean DFC — puede ser cache desactualizado).
    """
    found: dict[str, dict[str, str]]
    total_queried: int
    total_matched: int


@router.get("/dfc-pairs/lookup", response_model=DFCPairsLookupResponse)
async def dfc_pairs_lookup(
    db: DbDep,
    names: str = "",
) -> DFCPairsLookupResponse:
    """Busca varios nombres de golpe en el cache local (sin llamadas a Scryfall).

    ``names`` es una lista separada por ``|`` (evitamos ``,`` porque hay
    cartas con coma como "Bruna, the Fading Light").

    Uso: previews de import ("¿cuántas DFCs tiene este decklist antes de
    importar?"), analíticas del mazo, badges "// back" en la UI del picker.
    """
    parts = [n.strip() for n in names.split("|") if n.strip()]
    found = await dfc_pairs.bulk_lookup(db, parts)
    return DFCPairsLookupResponse(
        found=found,
        total_queried=len(parts),
        total_matched=len(found),
    )


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


# Se re-exporta para que `app.py` pueda incluirlo. Vive en un router aparte
# porque necesitamos que las URLs sean `/local-source/...` (sin el prefijo
# `/api` del router principal). Es el path que devuelven `download_url()`
# y `thumbnail_url()` de `LocalFolderSourceType`.
local_source_router = _local_source_router


# ============================================================================
# pHash cross-drive dedupe (Fase 2 · T8)
# ============================================================================

class PHashStatsResponse(BaseModel):
    available: bool
    """True si Pillow + imagehash están instalados."""
    enabled: bool
    """True si el setting `phash.enabled` está activado (y disponible)."""
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
    """Estado global del pHash: disponibilidad, cobertura del índice."""
    from sqlalchemy import func
    from mpc_forge.models import IndexedArt
    from mpc_forge.services import phash

    total = int(await db.scalar(select(func.count(IndexedArt.id))) or 0)
    with_hash = int(await db.scalar(
        select(func.count(IndexedArt.id)).where(IndexedArt.image_hash.is_not(None))
    ) or 0)
    return PHashStatsResponse(
        available=phash.is_available(),
        enabled=await phash.enabled(db),
        total_arts=total,
        with_hash=with_hash,
        coverage_pct=(100.0 * with_hash / total) if total else 0.0,
    )


async def _phash_compute_task(source_id: int, limit: int) -> None:
    """Background task que ejecuta el cálculo pHash con sesión propia."""
    from mpc_forge.services import phash
    try:
        async with session_scope() as db:
            # httpx client dedicado (sin depender del art_cache)
            import httpx
            from mpc_forge.ssl_config import ssl_insecure
            async with httpx.AsyncClient(
                timeout=15.0,
                verify=not ssl_insecure(),
                headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
            ) as client:
                stats = await phash.compute_missing_for_source(
                    db, client, source_id=source_id, limit=limit,
                )
                log.info("pHash retrofit source=%d: %s", source_id, stats)
    except Exception:  # noqa: BLE001
        log.exception("phash_compute_task falló para source %d", source_id)


@router.post("/drives/phash/compute", response_model=PHashComputeResponse)
async def phash_compute(
    payload: PHashComputeRequest,
    db: DbDep,
    background: BackgroundTasks,
    wait: bool = False,
) -> PHashComputeResponse:
    """Lanza el cálculo de pHash para artes de un source que aún no lo tengan.

    - ``wait=false`` (default): lanza background, responde inmediatamente
      con `computed=0`.
    - ``wait=true``: ejecuta inline hasta ``limit`` artes y devuelve stats.
      Útil para pruebas / retrofits pequeños.
    """
    from mpc_forge.services import phash
    if not phash.is_available():
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "pHash no disponible — instala `pip install Pillow imagehash`",
        )

    if not wait:
        background.add_task(_phash_compute_task, payload.source_id, payload.limit)
        return PHashComputeResponse(computed=0, failed=0, skipped=0,
                                    error="scheduled in background")

    # Inline execution — solo recomendable para limit pequeño.
    import httpx
    from mpc_forge.ssl_config import ssl_insecure
    async with httpx.AsyncClient(
        timeout=15.0,
        verify=not ssl_insecure(),
        headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
    ) as client:
        stats = await phash.compute_missing_for_source(
            db, client, source_id=payload.source_id, limit=payload.limit,
        )
    return PHashComputeResponse(**stats)


class PHashComputeAllRequest(BaseModel):
    """Retrofit pHash sobre TODOS los sources con artes sin hash.
    Limit es POR SOURCE, no global."""
    limit_per_source: int = Field(default=200, ge=1, le=5000)


async def _phash_compute_all_task(limit_per_source: int) -> None:
    """Background task que recorre todos los sources y calcula pHash para
    sus artes sin hash. Un `AsyncClient` compartido entre todos los
    sources para reutilizar conexiones.
    """
    from mpc_forge.services import phash
    from mpc_forge.models import ArtSource
    import httpx as _httpx
    from mpc_forge.ssl_config import ssl_insecure

    try:
        async with session_scope() as db:
            sources = (await db.scalars(select(ArtSource))).all()
        log.info("pHash retrofit-all: %d sources a procesar", len(sources))

        async with _httpx.AsyncClient(
            timeout=15.0,
            verify=not ssl_insecure(),
            headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
        ) as client:
            for src in sources:
                try:
                    async with session_scope() as db:
                        stats = await phash.compute_missing_for_source(
                            db, client, source_id=src.id, limit=limit_per_source,
                        )
                        log.info("pHash retrofit source=%d (%s): %s",
                                 src.id, src.name, stats)
                except Exception:  # noqa: BLE001
                    log.exception("pHash retrofit falló para source %d", src.id)
                    continue
    except Exception:  # noqa: BLE001
        log.exception("_phash_compute_all_task falló")


@router.post("/drives/phash/compute-all", response_model=dict[str, Any])
async def phash_compute_all(
    payload: PHashComputeAllRequest,
    background: BackgroundTasks,
) -> dict[str, Any]:
    """Extras · F2/T8: dispara un job de retrofit pHash sobre TODOS los sources.

    Útil como botón "Calcular pHash para todos los drives" en Ajustes. Es
    la mejor manera de aplicar pHash a los sources gdrive existentes sin
    reindexar (que sería mucho más lento por el fetch de la API de Drive).
    """
    from mpc_forge.services import phash
    if not phash.is_available():
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "pHash no disponible — instala `pip install Pillow imagehash`",
        )
    background.add_task(_phash_compute_all_task, payload.limit_per_source)
    return {"status": "scheduled",
            "message": "Job iniciado en background. Consulta /drives/phash/stats para ver progreso."}


@router.post("/drives/rebuild-fts5", response_model=dict[str, Any])
async def rebuild_fts5(db: DbDep) -> dict[str, Any]:
    """Extras · F2/T6: fuerza rebuild de la tabla virtual FTS5.

    Uso: si el usuario nota que la búsqueda está desincronizada (nuevas
    filas insertadas fuera del flujo del indexer, o si los triggers
    fallaron en algún batch por bug), este botón las regenera desde cero.

    Es idempotente y ejecuta en un solo statement SQL (`INSERT INTO fts VALUES
    ('rebuild')`). Para 100k artes tarda ~5-10s.
    """
    from sqlalchemy import text as sa_text
    from mpc_forge.models import KeyValue

    # Verificar que FTS5 está disponible primero
    kv = await db.get(KeyValue, "fts5_available")
    if not kv or kv.value != "1":
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "FTS5 no disponible en esta build de SQLite",
        )
    try:
        await db.execute(sa_text(
            "INSERT INTO indexed_art_fts(indexed_art_fts) VALUES ('rebuild')"
        ))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("Rebuild FTS5 falló")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Rebuild FTS5 falló: {e}",
        )
    return {"status": "ok", "message": "FTS5 rebuild completo"}


@router.post("/drives/canonical/validate", response_model=dict[str, int])
async def canonical_validate(
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    source_id: int | None = None,
    limit: int = 500,
) -> dict[str, int]:
    """Extras · F2/T5: valida y enriquece los `[SET NUM]` de los artes indexados.

    Consulta Scryfall en batch (75 identifiers por request) para verificar
    que las (set, num) capturadas del filename existen. Poblará
    `PrintingCache` para lookups posteriores del picker.
    """
    from mpc_forge.services import canonical as _canonical
    stats = await _canonical.validate_and_enrich(
        db, scryfall, limit=limit, source_id=source_id,
    )
    return stats


@router.get("/drives/canonical/details", response_model=dict[str, Any] | None)
async def canonical_details(
    db: DbDep,
    expansion_code: str,
    collector_number: str,
) -> dict[str, Any] | None:
    """Lookup local de detalles enriquecidos (artist, oracle_id, ...) para
    un (set, num). Devuelve null si no está en cache.
    """
    from mpc_forge.services import canonical as _canonical
    return await _canonical.get_canonical_details(
        db, expansion_code, collector_number,
    )


# ============================================================================
# Validate source antes de guardarlo (Extras · F2/T7)
# ============================================================================


class ValidateSourceRequest(BaseModel):
    """Payload de validación previo al POST /art-sources/."""
    url: str
    source_type: str | None = None
    """Opcional: forzar un tipo. Si es None, se auto-detecta."""


class ValidateSourceResponse(BaseModel):
    valid: bool
    detected_type: str
    canonical_url: str
    """URL normalizada por el source_type. Se recomienda usarla al guardar."""
    label: str
    error: str | None = None


@router.post("/tag-vocabulary/reload", response_model=dict[str, Any])
async def reload_tag_vocabulary() -> dict[str, Any]:
    """Extras · F1/T4: fuerza la recarga del vocabulario de tags custom.

    El usuario edita ``<data_dir>/tag_vocabulary.json`` y llama a este
    endpoint para que los cambios se apliquen sin reiniciar la app.
    NOTA: las filas ya indexadas mantienen sus tags viejos hasta el
    próximo reindex o el próximo bump de ``NORMALIZATION_VERSION``.
    """
    from mpc_forge.services.gdrive_indexer import reload_tag_vocabulary as _reload
    _reload()
    return {"status": "ok", "message": "Vocabulario recargado. Reindexa los drives para aplicar a los artes ya indexados."}


@router.get("/tag-vocabulary/current", response_model=dict[str, list[str]])
async def get_current_tag_vocabulary() -> dict[str, list[str]]:
    """Devuelve el vocabulario actualmente en uso (default + overrides)."""
    from mpc_forge.services.gdrive_indexer import _get_vocab
    vocab, _ = _get_vocab()
    return {canonical: sorted(aliases) for canonical, aliases in vocab.items()}


@router.post("/art-sources/validate", response_model=ValidateSourceResponse)
async def validate_source(payload: ValidateSourceRequest) -> ValidateSourceResponse:
    """Extras · F2/T7: comprueba si `url` es una source válida sin persistirla.

    Útil para la UI de "añadir source" — muestra al usuario feedback
    inmediato ("✓ Google Drive folder detectado", "✗ ruta local inexistente")
    antes de hacer el POST real.
    """
    from mpc_forge.services import art_sources as _asources
    from mpc_forge.services.source_types import resolve, list_registered

    raw = (payload.url or "").strip()
    if not raw:
        return ValidateSourceResponse(
            valid=False, detected_type="",
            canonical_url="", label="",
            error="URL vacía",
        )

    # Autodetección o forzar tipo
    if payload.source_type:
        type_cls = resolve(payload.source_type)
        if type_cls is None:
            valid_types = ", ".join(k for k, _ in list_registered())
            return ValidateSourceResponse(
                valid=False, detected_type=payload.source_type,
                canonical_url=raw, label="",
                error=f"Tipo desconocido: {payload.source_type!r}. Válidos: {valid_types}",
            )
        try:
            canonical = type_cls.validate_url(raw)
        except ValueError as e:
            return ValidateSourceResponse(
                valid=False, detected_type=payload.source_type,
                canonical_url=raw, label=type_cls.label, error=str(e),
            )
        return ValidateSourceResponse(
            valid=True, detected_type=payload.source_type,
            canonical_url=canonical, label=type_cls.label,
        )

    # Auto-detect
    try:
        detected_type, canonical = _asources._detect_source_type(raw)
    except Exception as e:  # noqa: BLE001
        return ValidateSourceResponse(
            valid=False, detected_type="",
            canonical_url=raw, label="",
            error=f"Error al detectar tipo: {e}",
        )

    type_cls = resolve(detected_type)
    label = type_cls.label if type_cls else "Otro"
    if detected_type == "other":
        return ValidateSourceResponse(
            valid=False, detected_type=detected_type,
            canonical_url=canonical, label=label,
            error="No se pudo detectar un tipo conocido. Elige uno manualmente.",
        )
    return ValidateSourceResponse(
        valid=True, detected_type=detected_type,
        canonical_url=canonical, label=label,
    )


@router.get("/drives/phash/similar/{file_id}", response_model=SimilarArtsResponse)
async def phash_similar(
    file_id: str,
    db: DbDep,
    threshold: int = 8,
    limit: int = 50,
) -> SimilarArtsResponse:
    """Encuentra artes similares (mismo pHash o muy cerca) al de ``file_id``.

    Uso: en el picker, un botón "Ver similares" muestra todos los duplicados
    cross-drive del arte actual.
    """
    from mpc_forge.models import IndexedArt
    from mpc_forge.services import phash

    reference = await db.scalar(
        select(IndexedArt).where(IndexedArt.file_id == file_id)
    )
    if not reference or not reference.image_hash:
        return SimilarArtsResponse(
            reference_file_id=file_id,
            reference_hash=None,
            similar=[],
        )
    similars = await phash.find_similar(
        db, reference.image_hash,
        threshold=max(0, min(32, threshold)),
        exclude_file_id=file_id,
        limit=limit,
    )
    # Serialización manual — no queremos exponer todos los campos.
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
