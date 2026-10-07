from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.art import art_library

log = logging.getLogger(__name__)

router = APIRouter(tags=["library"])


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _csv_ints(value: str | None) -> list[int]:
    out = []
    for item in _csv(value):
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out


def _filters_from_query(
    q: str,
    sources: str | None,
    variants: str | None,
    exclude: str | None,
    expansion: str,
    card_type: str,
    tags: str | None,
) -> art_library.LibraryFilters:
    return art_library.LibraryFilters(
        query=q,
        source_ids=_csv_ints(sources),
        variants=_csv(variants),
        exclude_variants=_csv(exclude),
        expansion_code=expansion,
        card_type=card_type,
        tags_include=_csv(tags),
    )


@router.get("/api/library/overview")
async def library_overview(db: DbDep) -> dict[str, Any]:
    return await art_library.overview(db)


@router.get("/api/library/browse")
async def library_browse(
    db: DbDep,
    q: str = Query("", max_length=200),
    sources: str | None = None,
    variants: str | None = None,
    exclude: str | None = None,
    expansion: str = Query("", max_length=16),
    card_type: str = Query("", max_length=16),
    tags: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(art_library.DEFAULT_PAGE_SIZE, ge=1, le=art_library.MAX_PAGE_SIZE),
    sort: str = Query(art_library.DEFAULT_SORT),
) -> dict[str, Any]:
    if sort not in art_library.SORT_OPTIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Orden invalido: {sort}. Validos: {', '.join(sorted(art_library.SORT_OPTIONS))}",
        )

    unknown = set(_csv(variants) + _csv(exclude)) - art_library.VARIANT_FLAGS.keys()
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Variantes desconocidas: {', '.join(sorted(unknown))}",
        )

    filters = _filters_from_query(q, sources, variants, exclude, expansion, card_type, tags)
    return await art_library.browse(db, filters, offset=offset, limit=limit, sort=sort)


@router.get("/api/library/facets")
async def library_facets(
    db: DbDep,
    q: str = Query("", max_length=200),
    sources: str | None = None,
    variants: str | None = None,
    exclude: str | None = None,
    expansion: str = Query("", max_length=16),
    card_type: str = Query("", max_length=16),
    tags: str | None = None,
) -> dict[str, Any]:
    filters = _filters_from_query(q, sources, variants, exclude, expansion, card_type, tags)
    return await art_library.facets(db, filters)


@router.get("/api/library/cards")
async def library_cards(
    db: DbDep,
    q: str = Query("", max_length=200),
    sources: str | None = None,
    variants: str | None = None,
    expansion: str = Query("", max_length=16),
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    filters = _filters_from_query(q, sources, variants, None, expansion, "", None)
    return {"cards": await art_library.distinct_names(db, filters, limit=limit)}


@router.get("/api/library/variants")
async def library_variants() -> dict[str, Any]:
    return {
        "variants": list(art_library.VARIANT_FLAGS.keys()),
        "sorts": list(art_library.SORT_OPTIONS.keys()),
    }
