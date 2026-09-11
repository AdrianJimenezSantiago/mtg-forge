"""Biblioteca de arte: explorar el índice de drives sin partir de una carta.

Qué faltaba
-----------
El proyecto tiene un indexador de decenas de drives de Google con FTS5, tags
derivados del nombre de fichero, metadatos canónicos ``[SET NUM]`` y hasta
pHash para deduplicar entre drives. Toda esa infraestructura solo era accesible
desde un modal, dentro del editor de un mazo, y siempre partiendo de una carta
concreta.

No había forma de responder preguntas que el índice ya podía contestar:
"¿qué hay de este artista?", "enséñame todo lo full art de Dominaria",
"¿qué tiene este drive que no tengan los demás?".

Diferencia con ``gdrive_search``
--------------------------------
``gdrive_search.search`` está optimizado para "dado el nombre de esta carta,
dame las mejores coincidencias": puntúa por similitud y devuelve un top-N. Aquí
se necesita lo contrario — navegar un conjunto grande con filtros y paginación
estable, sin ranking difuso. Un ``ORDER BY score`` haría que la página 2
mostrara resultados solapados con la 1 en cuanto cambiara cualquier cosa.

Por eso este módulo consulta ``IndexedArt`` directamente y ordena por columnas
deterministas, y solo delega en el buscador cuando hay texto de búsqueda.

Facetas
-------
Los contadores por drive, tag y expansión se calculan sobre el conjunto YA
filtrado, al contrario que en el selector de arte de una carta. Aquí el usuario
está explorando, no eligiendo: quiere saber "de lo que estoy viendo, cuánto hay
de cada cosa" para seguir acotando.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import ArtSource, IndexedArt
# Se reutilizan los constructores de URL del buscador en vez de duplicarlos:
# son la fuente de verdad de cómo se piden miniaturas y descargas a Drive, y
# tenerlos en dos sitios garantiza que algún día diverjan.
from mpc_forge.services.gdrive_search import _download_url, _thumb_url

log = logging.getLogger(__name__)

# Las columnas booleanas de variante, expuestas como facetas filtrables. El
# orden es el que se pinta en la interfaz.
VARIANT_FLAGS = {
    "full_art": IndexedArt.is_full_art,
    "borderless": IndexedArt.is_borderless,
    "extended": IndexedArt.is_extended,
    "showcase": IndexedArt.is_showcase,
    "retro": IndexedArt.is_retro,
    "textless": IndexedArt.is_textless,
    "promo": IndexedArt.is_promo,
    "alt_art": IndexedArt.is_alt_art,
}

# Ordenaciones admitidas. Todas terminan en `id` para que el orden sea TOTAL:
# sin desempate, dos filas con el mismo nombre pueden salir en distinto orden
# entre dos consultas y la paginación duplicaría o se saltaría elementos.
SORT_OPTIONS = {
    "name": (IndexedArt.name_normalized.asc(), IndexedArt.id.asc()),
    "name_desc": (IndexedArt.name_normalized.desc(), IndexedArt.id.desc()),
    "recent": (IndexedArt.indexed_at.desc(), IndexedArt.id.desc()),
    "oldest": (IndexedArt.indexed_at.asc(), IndexedArt.id.asc()),
    "set": (IndexedArt.expansion_code.asc(), IndexedArt.collector_number.asc(),
            IndexedArt.id.asc()),
    "size": (IndexedArt.size_bytes.desc(), IndexedArt.id.desc()),
}
DEFAULT_SORT = "name"

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 60


@dataclass
class LibraryFilters:
    """Estado de filtrado. Todo opcional; sin filtros se navega el índice."""
    query: str = ""
    source_ids: list[int] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)      # AND entre ellas
    exclude_variants: list[str] = field(default_factory=list)
    expansion_code: str = ""
    card_type: str = ""            # CARD | TOKEN | CARDBACK
    tags_include: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.query, self.source_ids, self.variants, self.exclude_variants,
            self.expansion_code, self.card_type, self.tags_include,
        ])


def _apply_filters(stmt: Select, filters: LibraryFilters) -> Select:
    """Traduce los filtros a cláusulas WHERE.

    Se comparte entre la consulta de resultados y las de facetas para que los
    contadores describan exactamente el mismo conjunto que la rejilla. Si
    divergieran, el usuario vería "12 full art" y al filtrar aparecerían 9.
    """
    if filters.source_ids:
        stmt = stmt.where(IndexedArt.source_id.in_(filters.source_ids))

    for name in filters.variants:
        column = VARIANT_FLAGS.get(name)
        if column is not None:
            stmt = stmt.where(column.is_(True))

    for name in filters.exclude_variants:
        column = VARIANT_FLAGS.get(name)
        if column is not None:
            stmt = stmt.where(column.is_(False))

    if filters.expansion_code:
        stmt = stmt.where(
            func.lower(IndexedArt.expansion_code) == filters.expansion_code.lower()
        )

    if filters.card_type:
        stmt = stmt.where(IndexedArt.card_type == filters.card_type.upper())

    for tag in filters.tags_include:
        # `tags` es una cadena separada por comas. Se envuelve en comas para que
        # buscar "art" no case con "alt_art" ni con "artist_proof".
        stmt = stmt.where(
            func.instr("," + IndexedArt.tags + ",", f",{tag},") > 0
        )

    if filters.query:
        # LIKE sobre el nombre normalizado. No se usa FTS5 aquí: el buscador
        # devuelve un ranking difuso y aquí hace falta un conjunto estable que
        # se pueda paginar y contar. Para "encuéntrame esta carta concreta" ya
        # está el selector de arte.
        needle = f"%{filters.query.strip().lower()}%"
        stmt = stmt.where(IndexedArt.name_normalized.like(needle))

    return stmt


def _serialize(row: IndexedArt, source_names: dict[int, str]) -> dict[str, Any]:
    return {
        "id": row.id,
        "file_id": row.file_id,
        "filename": row.filename,
        "source_id": row.source_id,
        "source_name": source_names.get(row.source_id, ""),
        "folder_path": row.folder_path,
        "size_bytes": row.size_bytes,
        "tags": [t for t in (row.tags or "").split(",") if t],
        "expansion_code": row.expansion_code,
        "collector_number": row.collector_number,
        "card_type": row.card_type,
        "image_hash": row.image_hash,
        # La columna `thumb_url` está vacía en la mayoría de las filas: solo
        # se rellena cuando el indexador la recibe del API de Drive, y con el
        # camino de scraping no llega. Hay que derivarla del file_id, que es
        # lo que hace el resto del proyecto. Leerla a pelo dejaba `src=""` y
        # la rejilla salía sin ninguna imagen.
        "thumb_url": row.thumb_url or _thumb_url(row.file_id),
        "download_url": row.download_url or _download_url(row.file_id),
        "variants": [
            name for name, column in VARIANT_FLAGS.items()
            if getattr(row, column.key, False)
        ],
        "indexed_at": row.indexed_at.isoformat() if row.indexed_at else None,
    }


async def browse(
    db: AsyncSession,
    filters: LibraryFilters,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    sort: str = DEFAULT_SORT,
) -> dict[str, Any]:
    """Una página de la biblioteca, con el total del conjunto filtrado."""
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    order = SORT_OPTIONS.get(sort, SORT_OPTIONS[DEFAULT_SORT])

    total = (await db.scalar(
        _apply_filters(select(func.count()).select_from(IndexedArt), filters)
    )) or 0

    stmt = _apply_filters(select(IndexedArt), filters)
    rows = (await db.execute(
        stmt.order_by(*order).offset(offset).limit(limit)
    )).scalars().all()

    source_names = await _source_names(db)

    return {
        "items": [_serialize(r, source_names) for r in rows],
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < int(total),
        "sort": sort if sort in SORT_OPTIONS else DEFAULT_SORT,
    }


async def _source_names(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(ArtSource.id, ArtSource.name))).all()
    return {r[0]: r[1] for r in rows}


async def facets(
    db: AsyncSession, filters: LibraryFilters, *, top_expansions: int = 40
) -> dict[str, Any]:
    """Contadores del conjunto filtrado, para ir acotando la exploración."""
    source_names = await _source_names(db)

    by_source = [
        {
            "source_id": int(row[0]),
            "name": source_names.get(int(row[0]), f"Drive {row[0]}"),
            "count": int(row[1]),
        }
        for row in (await db.execute(
            _apply_filters(
                select(IndexedArt.source_id, func.count()), filters
            ).group_by(IndexedArt.source_id).order_by(func.count().desc())
        )).all()
    ]

    by_variant = {}
    for name, column in VARIANT_FLAGS.items():
        count = (await db.scalar(
            _apply_filters(
                select(func.count()).select_from(IndexedArt), filters
            ).where(column.is_(True))
        )) or 0
        by_variant[name] = int(count)

    by_expansion = [
        {"code": row[0], "count": int(row[1])}
        for row in (await db.execute(
            _apply_filters(
                select(IndexedArt.expansion_code, func.count()), filters
            )
            .where(IndexedArt.expansion_code.isnot(None))
            .where(IndexedArt.expansion_code != "")
            .group_by(IndexedArt.expansion_code)
            .order_by(func.count().desc())
            .limit(top_expansions)
        )).all()
    ]

    by_card_type = [
        {"type": row[0] or "CARD", "count": int(row[1])}
        for row in (await db.execute(
            _apply_filters(
                select(IndexedArt.card_type, func.count()), filters
            ).group_by(IndexedArt.card_type).order_by(func.count().desc())
        )).all()
    ]

    return {
        "by_source": by_source,
        "by_variant": by_variant,
        "by_expansion": by_expansion,
        "by_card_type": by_card_type,
    }


async def overview(db: AsyncSession) -> dict[str, Any]:
    """Cifras globales de la biblioteca, para la cabecera de la vista."""
    total = (await db.scalar(select(func.count()).select_from(IndexedArt))) or 0
    sources = (await db.scalar(
        select(func.count(func.distinct(IndexedArt.source_id)))
    )) or 0
    expansions = (await db.scalar(
        select(func.count(func.distinct(IndexedArt.expansion_code)))
        .where(IndexedArt.expansion_code.isnot(None))
        .where(IndexedArt.expansion_code != "")
    )) or 0
    hashed = (await db.scalar(
        select(func.count()).select_from(IndexedArt)
        .where(IndexedArt.image_hash.isnot(None))
    )) or 0
    total_bytes = (await db.scalar(
        select(func.coalesce(func.sum(IndexedArt.size_bytes), 0))
    )) or 0

    return {
        "total_arts": int(total),
        "sources": int(sources),
        "expansions": int(expansions),
        "with_phash": int(hashed),
        "total_bytes": int(total_bytes),
        "is_empty": int(total) == 0,
    }


async def distinct_names(
    db: AsyncSession, filters: LibraryFilters, *, limit: int = 500
) -> list[dict[str, Any]]:
    """Nombres de carta distintos dentro del filtro, con cuántas versiones hay.

    Es la vista "por carta" de la biblioteca: en lugar de una rejilla de
    ficheros sueltos, agrupa las 14 versiones de Sol Ring repartidas por seis
    drives en una sola entrada.
    """
    rows = (await db.execute(
        _apply_filters(
            select(
                IndexedArt.name_normalized,
                func.count().label("versions"),
                func.count(func.distinct(IndexedArt.source_id)).label("sources"),
                func.min(IndexedArt.filename).label("sample"),
            ),
            filters,
        )
        .group_by(IndexedArt.name_normalized)
        .order_by(func.count().desc(), IndexedArt.name_normalized.asc())
        .limit(limit)
    )).all()

    return [
        {
            "name": row[0],
            "versions": int(row[1]),
            "sources": int(row[2]),
            "sample_filename": row[3],
        }
        for row in rows
    ]
