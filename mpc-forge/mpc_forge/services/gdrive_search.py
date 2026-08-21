"""Búsqueda fuzzy de artes en el índice de drives.

Estrategia:
- Prefiltro SQL con LIKE contra el nombre normalizado (rápido, elimina el 90%
  del ruido). Si hay pocos resultados, ampliamos con LIKE partido por tokens.
- Ranking fino con rapidfuzz.WRatio para tolerar variantes del nombre:
  "Sol Ring", "Sol Ring (Anime)", "Sol Ring - Alt Art", etc. Todos matchean bien.
- Devolvemos los top-N con score, ordenados por relevancia.

Cada resultado incluye URLs listas para consumir:
- thumb_url: para vista previa en la UI (Google renderiza automáticamente)
- download_url: para el flujo "+ Añadir por URL" ya existente
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.services.gdrive_indexer import normalize_filename

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    file_id: str
    filename: str
    source_id: int
    source_name: str
    folder_path: str
    thumb_url: str
    download_url: str
    score: int  # 0-100


def _thumb_url(file_id: str, size: int = 300) -> str:
    """URL de thumbnail servida por Google. `sz=w300` funciona hasta ~1024."""
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w{size}"


def _download_url(file_id: str) -> str:
    """URL de descarga directa (funciona para archivos < ~100MB sin token extra)."""
    return f"https://drive.google.com/uc?id={file_id}&export=download"


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------

# Umbral mínimo de similitud (0-100). Con WRatio, 65 es un match aceptable,
# 80+ es muy bueno. Filtramos ruido con 55.
_MIN_SCORE = 55
# Candidatos que pedimos al SQL antes del re-rank fino. Suficientemente grande
# para no perder buenos matches, no tanto que ralentice.
_SQL_PREFETCH = 500


async def search(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    source_ids: list[int] | None = None,
) -> list[SearchResult]:
    """Busca `query` en el índice y devuelve top-N por relevancia.

    Args:
        query: nombre de carta o parte de él ("sol ring", "delver", "brisela top")
        limit: nº máximo de resultados a devolver
        source_ids: opcional, restringir a estos sources
    """
    q_norm = normalize_filename(query)
    if not q_norm:
        return []

    # --- Prefiltro SQL ---
    # 1. Match "empieza por el primer token" (rápido, LIKE con prefijo)
    tokens = q_norm.split()
    stmt = select(IndexedArt, ArtSource.name).join(
        ArtSource, ArtSource.id == IndexedArt.source_id
    )
    if source_ids:
        stmt = stmt.where(IndexedArt.source_id.in_(source_ids))

    # LIKE por cualquier token principal (>2 chars)
    long_tokens = [t for t in tokens if len(t) > 2]
    if long_tokens:
        conds = [IndexedArt.name_normalized.like(f"%{t}%") for t in long_tokens]
        stmt = stmt.where(or_(*conds))
    else:
        stmt = stmt.where(IndexedArt.name_normalized.like(f"%{q_norm}%"))

    stmt = stmt.limit(_SQL_PREFETCH)
    rows = (await db.execute(stmt)).all()

    if not rows:
        return []

    # --- Re-rank fino con rapidfuzz ---
    # WRatio tolera bien las variantes ("Sol Ring (Anime)" vs "sol ring")
    candidates = [(r[0], r[1]) for r in rows]
    scored = process.extract(
        q_norm,
        {i: art.name_normalized for i, (art, _) in enumerate(candidates)},
        scorer=fuzz.WRatio,
        limit=limit * 3,  # tomamos más y filtramos por umbral
    )

    results: list[SearchResult] = []
    for _match_str, score, idx in scored:
        if score < _MIN_SCORE:
            continue
        art, source_name = candidates[idx]
        results.append(SearchResult(
            file_id=art.file_id,
            filename=art.filename,
            source_id=art.source_id,
            source_name=source_name,
            folder_path=art.folder_path,
            thumb_url=_thumb_url(art.file_id),
            download_url=_download_url(art.file_id),
            score=int(score),
        ))
        if len(results) >= limit:
            break

    return results


async def stats(db: AsyncSession) -> dict:
    """Estadísticas globales del índice para mostrar en la UI."""
    from sqlalchemy import func
    total = int(await db.scalar(select(func.count(IndexedArt.id))) or 0)
    sources_with_index = int(await db.scalar(
        select(func.count(func.distinct(IndexedArt.source_id)))
    ) or 0)
    return {
        "total_files": total,
        "sources_indexed": sources_with_index,
    }
