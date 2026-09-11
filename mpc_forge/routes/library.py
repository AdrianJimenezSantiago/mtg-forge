"""Endpoints de la biblioteca de arte y del asistente de calibración.

Ambos exponen infraestructura que ya existía pero no tenía superficie: el
índice de drives solo era accesible desde el selector de una carta, y los
offsets de dúplex había que adivinarlos a mano.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.db import get_session
from mpc_forge.services import art_library, calibration

log = logging.getLogger(__name__)
DbDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(tags=["library"])


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _csv_ints(value: str | None) -> list[int]:
    out = []
    for item in _csv(value):
        try:
            out.append(int(item))
        except ValueError:
            # Un id no numérico en la query se ignora en vez de devolver 422:
            # viene de la URL, que el usuario puede haber editado o compartido.
            continue
    return out


def _filters_from_query(
    q: str, sources: str | None, variants: str | None,
    exclude: str | None, expansion: str, card_type: str, tags: str | None,
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


# ---------------------------------------------------------------------------
# Biblioteca de arte
# ---------------------------------------------------------------------------

@router.get("/api/library/overview")
async def library_overview(db: DbDep) -> dict[str, Any]:
    """Cifras globales del índice. Alimenta la cabecera de la vista."""
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
    limit: int = Query(art_library.DEFAULT_PAGE_SIZE, ge=1,
                       le=art_library.MAX_PAGE_SIZE),
    sort: str = Query(art_library.DEFAULT_SORT),
) -> dict[str, Any]:
    """Una página de la biblioteca.

    Los filtros llegan como listas separadas por comas para que la URL sea
    compartible y se pueda guardar en marcadores: la vista escribe el estado
    de filtrado en la barra de direcciones.
    """
    if sort not in art_library.SORT_OPTIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Orden invalido: {sort}. Validos: "
            f"{', '.join(sorted(art_library.SORT_OPTIONS))}",
        )

    unknown = set(_csv(variants) + _csv(exclude)) - art_library.VARIANT_FLAGS.keys()
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Variantes desconocidas: {', '.join(sorted(unknown))}",
        )

    filters = _filters_from_query(
        q, sources, variants, exclude, expansion, card_type, tags
    )
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
    """Contadores del conjunto filtrado.

    A diferencia del selector de arte de una carta, aquí las facetas SÍ se
    calculan sobre lo ya filtrado: el usuario está explorando y quiere saber
    "de lo que estoy viendo, cuánto hay de cada cosa" para seguir acotando.
    """
    filters = _filters_from_query(
        q, sources, variants, exclude, expansion, card_type, tags
    )
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
    """Vista agrupada por carta en lugar de por fichero.

    Agrupa las catorce versiones de Sol Ring repartidas por seis drives en una
    sola entrada con su contador.
    """
    filters = _filters_from_query(q, sources, variants, None, expansion, "", None)
    return {"cards": await art_library.distinct_names(db, filters, limit=limit)}


@router.get("/api/library/variants")
async def library_variants() -> dict[str, Any]:
    """Facetas de variante disponibles. Evita duplicar la lista en el frontend."""
    return {"variants": list(art_library.VARIANT_FLAGS.keys()),
            "sorts": list(art_library.SORT_OPTIONS.keys())}


# ---------------------------------------------------------------------------
# Calibración de dúplex
# ---------------------------------------------------------------------------

class CalibrationRequest(BaseModel):
    """Lo que el usuario ha medido en la hoja impresa."""
    # Rango generoso: si alguien mide 40 mm es que algo va muy mal, pero
    # rechazarlo de plano no le ayuda a entender qué. El servicio devuelve un
    # aviso en su lugar.
    measured_x_mm: float = Field(..., ge=-50, le=50)
    measured_y_mm: float = Field(..., ge=-50, le=50)
    flip_edge: Literal["long", "short"] = "long"


@router.post("/api/calibration/derive")
async def derive_calibration(payload: CalibrationRequest) -> dict[str, Any]:
    """Convierte la medición en los offsets del PDF.

    El cálculo del signo lo hace el servidor a propósito: es donde se equivoca
    todo el mundo, y pedirle al usuario que razone sobre el espejado del dúplex
    convertiría el asistente en otro problema.
    """
    result = calibration.derive_offsets(
        payload.measured_x_mm,
        payload.measured_y_mm,
        flip_edge=payload.flip_edge,
    )
    return calibration.explain(result)


@router.get("/api/calibration/sheet")
async def calibration_sheet(
    page_size: str = Query("a4"),
    flip_edge: Literal["long", "short"] = "long",
) -> FileResponse:
    """Genera y devuelve el PDF de calibración de dos páginas."""
    if page_size.lower() not in ("a4", "letter"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Tamaño de página no soportado"
        )
    # ReportLab es bloqueante; sin to_thread se congelaría el event loop.
    path = await asyncio.to_thread(
        calibration.build_sheet, None, page_size=page_size, flip_edge=flip_edge
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="calibracion-duplex.pdf",
        # `no-store`: la hoja se regenera en cada petición según el tamaño y el
        # borde de volteo elegidos, y servir una versión cacheada con la
        # configuración anterior arruinaría la calibración en silencio.
        headers={"Cache-Control": "no-store"},
    )
