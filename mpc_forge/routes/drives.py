from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from mpc_forge.models import ArtSource, KeyValue
from mpc_forge.routes.dependencies import DbDep, ScryfallDep
from mpc_forge.services.cards import canonical
from mpc_forge.services.indexing import gdrive_indexer, gdrive_search
from mpc_forge.services.indexing.index_queue import get_queue as get_index_queue

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])


class IndexBatchRequest(BaseModel):
    source_ids: list[int] = Field(..., min_length=1)
    mode: str = Field(
        default="pending",
        description=(
            "'all': indexa todos los IDs recibidos. "
            "'pending': solo los que no tienen indexed_at (skip ya indexados). "
            "'pinned': solo los marcados como pinned."
        ),
    )


class IndexBatchResponse(BaseModel):
    batch_id: str
    queued: int
    skipped: int


@router.post("/drives/index-batch", response_model=IndexBatchResponse)
async def index_batch(payload: IndexBatchRequest, db: DbDep) -> IndexBatchResponse:
    queue = get_index_queue()

    if queue.is_running():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Ya hay un indexado en curso. Espera a que termine o cancélalo "
            "con POST /drives/index-cancel.",
        )

    found = {
        src.id: src
        for src in (
            await db.scalars(select(ArtSource).where(ArtSource.id.in_(payload.source_ids)))
        ).all()
    }
    sources_by_id = {sid: found[sid] for sid in dict.fromkeys(payload.source_ids) if sid in found}

    final_ids: list[int] = []
    skipped = 0
    for sid, src in sources_by_id.items():
        if payload.mode == "pending" and src.indexed_at is not None:
            skipped += 1
            continue
        if payload.mode == "pinned" and not src.pinned:
            skipped += 1
            continue
        if src.source_type == "gdrive-file":
            skipped += 1
            continue
        final_ids.append(sid)

    if not final_ids:
        return IndexBatchResponse(batch_id="", queued=0, skipped=skipped)

    names = {sid: src.name for sid, src in sources_by_id.items()}
    result = await queue.enqueue(final_ids, names=names)

    return IndexBatchResponse(
        batch_id=result["batch_id"],
        queued=result["queued"],
        skipped=skipped,
    )


@router.get("/drives/index-progress")
async def index_progress() -> dict[str, Any]:
    return get_index_queue().progress()


@router.post("/drives/index-cancel")
async def index_cancel() -> dict[str, Any]:
    queue = get_index_queue()
    if not queue.is_running():
        return {"cancelled": 0, "message": "No hay indexado en curso."}
    return queue.cancel()


@router.post("/drives/index-clear")
async def index_clear() -> dict[str, str]:
    get_index_queue().clear_completed()
    return {"status": "ok"}


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
    expansion_code: str | None = None
    collector_number: str | None = None
    image_hash: str | None = None

    @classmethod
    def from_result(cls, r: gdrive_search.SearchResult) -> SearchHit:
        return cls(
            file_id=r.file_id,
            filename=r.filename,
            source_id=r.source_id,
            source_name=r.source_name,
            folder_path=r.folder_path,
            thumb_url=r.thumb_url,
            download_url=r.download_url,
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
            image_hash=r.image_hash,
        )


def _parse_csv_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


DRIVE_SEARCH_MAX_PAGE = 500


@router.get("/drives/search", response_model=list[SearchHit])
async def drives_search(
    q: str,
    db: DbDep,
    response: Response,
    limit: int = Query(20, ge=1, le=DRIVE_SEARCH_MAX_PAGE),
    offset: int = Query(0, ge=0),
    source_id: int | None = None,
    tags_include: str | None = None,
    tags_exclude: str | None = None,
    expansion_code: str | None = None,
) -> list[SearchHit]:
    if not q.strip():
        response.headers["X-Total-Count"] = "0"
        return []
    source_ids = [source_id] if source_id else None
    page = await gdrive_search.search_page(
        db,
        q,
        limit=limit,
        offset=offset,
        source_ids=source_ids,
        tags_include=_parse_csv_list(tags_include),
        tags_exclude=_parse_csv_list(tags_exclude),
        expansion_code=(expansion_code or None),
    )
    response.headers["X-Total-Count"] = str(page.total)
    if page.capped:
        response.headers["X-Total-Capped"] = "1"
    results = page.results
    return [SearchHit.from_result(r) for r in results]


@router.get("/drives/cardbacks", response_model=list[SearchHit])
async def drives_cardbacks(
    db: DbDep,
    limit: int = 100,
    source_id: int | None = None,
) -> list[SearchHit]:
    source_ids = [source_id] if source_id else None
    results = await gdrive_search.list_cardbacks(db, limit=limit, source_ids=source_ids)
    return [SearchHit.from_result(r) for r in results]


class DriveStatsResponse(BaseModel):
    total_files: int
    sources_indexed: int
    fts5_available: bool = False


@router.get("/drives/stats", response_model=DriveStatsResponse)
async def drives_stats(db: DbDep) -> DriveStatsResponse:
    s = await gdrive_search.stats(db)
    return DriveStatsResponse(**s)


@router.post("/drives/rebuild-fts5", response_model=dict[str, Any])
async def rebuild_fts5(db: DbDep) -> dict[str, Any]:
    kv = await db.get(KeyValue, "fts5_available")
    if not kv or kv.value != "1":
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "FTS5 no disponible en esta build de SQLite",
        )
    try:
        await db.execute(text("INSERT INTO indexed_art_fts(indexed_art_fts) VALUES ('rebuild')"))
        await db.commit()
    except Exception as e:
        log.exception("Rebuild FTS5 falló")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Rebuild FTS5 falló: {e}",
        ) from e
    return {"status": "ok", "message": "FTS5 rebuild completo"}


@router.post("/drives/canonical/validate", response_model=dict[str, int])
async def canonical_validate(
    db: DbDep,
    scryfall: ScryfallDep,
    source_id: int | None = None,
    limit: int = 500,
) -> dict[str, int]:
    stats = await canonical.validate_and_enrich(
        db,
        scryfall,
        limit=limit,
        source_id=source_id,
    )
    return stats


@router.get("/drives/canonical/details", response_model=dict[str, Any] | None)
async def canonical_details(
    db: DbDep,
    expansion_code: str,
    collector_number: str,
) -> dict[str, Any] | None:
    return await canonical.get_canonical_details(
        db,
        expansion_code,
        collector_number,
    )


@router.post("/tag-vocabulary/reload", response_model=dict[str, Any])
async def reload_tag_vocabulary() -> dict[str, Any]:
    gdrive_indexer.reload_tag_vocabulary()
    return {
        "status": "ok",
        "message": "Vocabulario recargado. Reindexa los drives para aplicar a los artes ya indexados.",
    }


@router.get("/tag-vocabulary/current", response_model=dict[str, list[str]])
async def get_current_tag_vocabulary() -> dict[str, list[str]]:
    vocab, _ = gdrive_indexer._get_vocab()
    return {tag: sorted(aliases) for tag, aliases in vocab.items()}
