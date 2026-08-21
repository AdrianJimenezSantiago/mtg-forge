"""Búsqueda de artes en el índice de drives.

Filosofía:
- Los filenames se han normalizado agresivamente al indexar: "Forest.png",
  "Forest (Full Art).png" y "Forest - by Chowning.png" todos son "forest".
- Un match útil es: nombre_normalizado == query_normalizado (exacto).
- Nunca queremos "Forest Warden" cuando el usuario busca "Forest".
- Aceptamos algo de tolerancia por typos con rapidfuzz solo cuando el match
  exacto no da suficientes resultados.

Rendimiento:
- Prefijo primero (`LIKE 'query%'`) — SQLite usa el índice sobre name_normalized,
  es prácticamente instantáneo aunque tengamos 500k filas.
- Solo si eso da 0 resultados hacemos LIKE '%query%' (más lento) como fallback.
- Rapidfuzz solo se aplica sobre el conjunto pequeño ya prefiltrado.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz
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
# Scoring
# ---------------------------------------------------------------------------

def _score_match(query_norm: str, name_norm: str) -> int:
    """Puntúa la similitud entre query y nombre normalizado (0-100).

    Idea clave: **noise_ratio** = (tokens extra en el filename) / (tokens del query).
    Un token extra sobre un query de 1 palabra es 100% ruido → probablemente
    otra carta. Un token extra sobre query de 4 palabras es solo 25% ruido →
    probablemente variante de arte del mismo nombre.

    Ejemplos:
      "forest" vs "forest"          → 100 (exacto)
      "forest" vs "forest warden"   → noise 1/1 = 1.0 → 40 (bajo threshold, oculto)
      "sol ring" vs "cursed sol ring" → noise 1/2 = 0.5 → 60 (aparece pero abajo)
      "bruna the fading light" vs "bruna the fading light retro" → noise 1/4 = 0.25 → 90
    """
    if not query_norm or not name_norm:
        return 0

    # 1. Match exacto — caso ideal
    if query_norm == name_norm:
        return 100

    q_tokens = query_norm.split()
    n_tokens = name_norm.split()
    q_set = set(q_tokens)
    n_set = set(n_tokens)
    q_len = max(len(q_tokens), 1)

    def _by_noise_ratio(extra: int) -> int:
        """Score basado en cuánto del query es proporcionalmente 'ruido' extra."""
        noise_ratio = extra / q_len
        if noise_ratio == 0:
            return 100
        elif noise_ratio <= 0.25:  # 1 extra sobre 4+ tokens
            return 90
        elif noise_ratio <= 0.5:   # 1 extra sobre 2 tokens, o 2 sobre 4
            return 60
        elif noise_ratio <= 1.0:   # 1 extra sobre 1 token → seguramente otra carta
            return 40  # queda bajo _MIN_SCORE=55 y se filtra
        else:
            return 0

    # 2. Query es subset de tokens del filename (prefix o desordenado)
    if q_set.issubset(n_set):
        # Bonus si además va como prefijo consecutivo (más "canónico")
        if name_norm.startswith(query_norm + " ") or name_norm == query_norm:
            base = _by_noise_ratio(len(n_tokens) - len(q_tokens))
            return min(base + 5, 100)  # pequeño bonus por prefix
        return _by_noise_ratio(len(n_set) - len(q_set))

    # 3. Fallback: rapidfuzz para tolerar typos (Sol Rong → Sol Ring)
    r = int(fuzz.ratio(query_norm, name_norm))
    if r >= 85:
        return r
    return 0


# ---------------------------------------------------------------------------
# Búsqueda
# ---------------------------------------------------------------------------

# Umbral mínimo: 55. Filtramos "casi-matches" ruidosos.
_MIN_SCORE = 55
# Prefetch inicial: cuántos candidatos pedimos al SQL antes de re-score.
# Con prefix match y índice usado, esto es rápido incluso con números altos.
_SQL_PREFETCH = 300


async def search(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    source_ids: list[int] | None = None,
) -> list[SearchResult]:
    """Busca `query` en el índice y devuelve top-N por relevancia.

    Prefiltro SQL en tres fases (parando en la primera que dé resultados):
      1. Match exacto por name_normalized (usa índice)
      2. Prefix (LIKE 'query%') sobre name_normalized (usa índice)
      3. LIKE '%query%' como fallback (más lento pero necesario cuando el nombre
         tiene tokens delante, ej. "sol ring" busca en "cursed sol ring")
    """
    q_norm = normalize_filename(query)
    if not q_norm:
        return []

    base = select(IndexedArt, ArtSource.name).join(
        ArtSource, ArtSource.id == IndexedArt.source_id
    )
    if source_ids:
        base = base.where(IndexedArt.source_id.in_(source_ids))

    # --- Fase 1: match exacto (súper rápido, índice B-tree) ---
    stmt = base.where(IndexedArt.name_normalized == q_norm).limit(_SQL_PREFETCH)
    rows = (await db.execute(stmt)).all()

    # --- Fase 2: prefix (rápido, sí usa índice) ---
    if len(rows) < 20:
        stmt = base.where(
            IndexedArt.name_normalized.like(f"{q_norm}%"),
            IndexedArt.name_normalized != q_norm,  # no duplicar los ya encontrados
        ).limit(_SQL_PREFETCH - len(rows))
        rows += (await db.execute(stmt)).all()

    # --- Fase 3: substring en cualquier posición (más lento, solo si hace falta) ---
    if len(rows) < 20:
        # Solo si el query tiene >=3 chars (evitar '%a%' que barre toda la BD)
        long_tokens = [t for t in q_norm.split() if len(t) >= 3]
        if long_tokens:
            conds = [IndexedArt.name_normalized.like(f"%{t}%") for t in long_tokens]
            stmt = base.where(
                or_(*conds),
                ~IndexedArt.name_normalized.like(f"{q_norm}%"),
                IndexedArt.name_normalized != q_norm,
            ).limit(_SQL_PREFETCH - len(rows))
            rows += (await db.execute(stmt)).all()

    if not rows:
        return []

    # --- Re-scoring y ordenación ---
    scored: list[tuple[int, SearchResult]] = []
    for art, source_name in rows:
        s = _score_match(q_norm, art.name_normalized)
        if s < _MIN_SCORE:
            continue
        scored.append((s, SearchResult(
            file_id=art.file_id,
            filename=art.filename,
            source_id=art.source_id,
            source_name=source_name,
            folder_path=art.folder_path,
            thumb_url=_thumb_url(art.file_id),
            download_url=_download_url(art.file_id),
            score=s,
        )))

    scored.sort(key=lambda x: (-x[0], x[1].filename))
    return [r for _, r in scored[:limit]]


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
