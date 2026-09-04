"""Búsqueda de artes en el índice de drives.

Filosofía:
- Los filenames se han normalizado agresivamente al indexar: "Forest.png",
  "Forest (Full Art).png" y "Forest - by Chowning.png" todos son "forest".
- Un match útil es: nombre_normalizado == query_normalizado (exacto).
- Nunca queremos "Forest Warden" cuando el usuario busca "Forest".
- Aceptamos algo de tolerancia por typos con rapidfuzz solo cuando el match
  exacto no da suficientes resultados.

Query pipeline:
- El query del usuario pasa por ``normalize_filename()`` (misma función usada
  al indexar). Esto garantiza que "Jaya", "jaya" y "Jayā" matchean todos a
  la misma canonical form. Ver `gdrive_indexer.normalize_filename` para la
  pipeline completa (asciifolding + lowercase + strip de variante/paréntesis).

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
    tags: list[str]  # canonical tags extraídos del filename/folder ("full_art", …)
    is_full_art: bool = False
    is_borderless: bool = False
    is_extended: bool = False
    is_showcase: bool = False
    is_retro: bool = False
    is_textless: bool = False
    is_promo: bool = False
    is_alt_art: bool = False
    # Metadatos canónicos (Fase 2 · T5) — presentes solo si el arte lleva
    # tag `[SET NUM]` en filename o folder_path.
    expansion_code: str | None = None
    collector_number: str | None = None
    # Extras · F2/T8: perceptual hash (pHash) para dedupe cross-drive.
    # NULL si no se ha calculado.
    image_hash: str | None = None


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


# Cache del flag FTS5 tras primera consulta. Es un valor set-once por el
# startup (`init_db`); no cambia en runtime, así que un módulo-global cache
# elimina el read del KV por cada búsqueda. Un `None` significa "aún no
# comprobado" (se resuelve en la primera llamada a `_fts5_available()`).
_fts5_available_cache: bool | None = None


async def _fts5_available(db: AsyncSession) -> bool:
    """Lee el flag persistido por `init_db._try_setup_fts5()`.

    Se cachea a nivel de módulo. Si el KV falta (BD vieja pre-Fase 2 sin
    kv_store poblado), devuelve False de forma segura → fallback a LIKE.
    """
    global _fts5_available_cache
    if _fts5_available_cache is not None:
        return _fts5_available_cache
    try:
        from mpc_forge.models import KeyValue
        kv = await db.get(KeyValue, "fts5_available")
        _fts5_available_cache = bool(kv and kv.value == "1")
    except Exception:  # noqa: BLE001
        _fts5_available_cache = False
    return _fts5_available_cache


def _fts_escape(query: str) -> str:
    """Escapa un query para FTS5 MATCH.

    Convierte "sol ring" en '"sol" "ring"*' — cada token entre comillas
    (evita interpretaciones de operadores como AND/OR/NEAR) y el último con
    ``*`` para permitir prefix matching (útil cuando el usuario escribe
    parcialmente: "elesh" → matchea "elesh norn").

    Devuelve string vacío si no hay tokens válidos, lo que el caller debe
    tratar como "no match" y fallar a la fase LIKE.
    """
    # Solo alfanumérico (name_normalized ya está normalizado). Split por
    # espacios. Excluir tokens vacíos.
    tokens = [t for t in query.split() if t]
    if not tokens:
        return ""
    # Cada token → "token" (comillas dobles escapadas cambiando " por "").
    # El último token lleva prefix operator "*" para matching parcial.
    parts = []
    for i, tok in enumerate(tokens):
        safe = tok.replace('"', '""')
        if i == len(tokens) - 1:
            parts.append(f'"{safe}"*')
        else:
            parts.append(f'"{safe}"')
    return " ".join(parts)


async def _search_fts5(
    db: AsyncSession,
    q_norm: str,
    limit: int,
    source_ids: list[int] | None,
    tags_include: list[str] | None,
    tags_exclude: list[str] | None,
    expansion_code: str | None,
) -> list[SearchResult]:
    """Búsqueda vía tabla virtual FTS5 con ranking BM25.

    JOIN entre `indexed_art_fts` (que devuelve rowid ordenado por bm25) y la
    tabla real `indexed_art` para traer todas las columnas + `art_sources`
    para el nombre del source.

    Los filtros por tag / source / expansion se aplican en el WHERE del JOIN,
    no en el MATCH — SQLite es eficiente combinando ambos (usa el índice
    invertido para la primera cardinalidad, luego filtra).

    Devuelve directamente `SearchResult` ordenados por bm25 (menor = mejor).
    Traducimos el bm25 a la misma escala 0-100 que `_score_match` para no
    romper contratos con el caller.
    """
    fts_query = _fts_escape(q_norm)
    if not fts_query:
        return []

    # WHERE clauses estáticas — construimos con SQL crudo para poder mezclar
    # con MATCH que no expone directamente vía ORM.
    where_parts = ["indexed_art_fts MATCH :match"]
    params: dict = {"match": fts_query, "limit": _SQL_PREFETCH}

    if source_ids:
        # SQLite acepta parámetros expandidos si los damos como tuple + ejecutamos
        # con la sintaxis IN (...). SQLAlchemy `text` expandirá `bindparam`
        # con `expanding=True`, pero para simplicidad hacemos el interpolado
        # de ints (safe por ser enteros validados).
        ids = ",".join(str(int(x)) for x in source_ids)
        where_parts.append(f"ia.source_id IN ({ids})")

    if expansion_code:
        where_parts.append("ia.expansion_code = :exp_code")
        params["exp_code"] = expansion_code.lower()

    _TAG_TO_COL = {
        "full_art": "is_full_art", "borderless": "is_borderless",
        "extended": "is_extended", "showcase": "is_showcase",
        "retro":    "is_retro",    "textless":  "is_textless",
        "promo":    "is_promo",    "alt_art":   "is_alt_art",
    }
    if tags_include:
        for t in tags_include:
            col = _TAG_TO_COL.get(t)
            if col:
                where_parts.append(f"ia.{col} = 1")
    if tags_exclude:
        for t in tags_exclude:
            col = _TAG_TO_COL.get(t)
            if col:
                where_parts.append(f"ia.{col} = 0")

    where_sql = " AND ".join(where_parts)

    # bm25() de FTS5: menor = mejor. La 2ª columna del CREATE VIRTUAL TABLE
    # tiene mayor peso implícito, pero preferimos que name_normalized (col 0)
    # domine — bm25 acepta pesos por columna. Damos peso alto a
    # name_normalized (10), medio a filename (5), bajo a tags (1).
    sql = f"""
        SELECT ia.*, s.name AS source_name,
               bm25(indexed_art_fts, 10.0, 5.0, 1.0) AS rank
        FROM indexed_art_fts
        JOIN indexed_art AS ia ON ia.id = indexed_art_fts.rowid
        JOIN art_sources AS s  ON s.id = ia.source_id
        WHERE {where_sql}
        ORDER BY rank
        LIMIT :limit
    """
    from sqlalchemy import text as sa_text
    result = await db.execute(sa_text(sql), params)
    rows = result.mappings().all()
    if not rows:
        return []

    # Traducir bm25 → 0-100. bm25 típico de un exact match es negativo cercano
    # a 0 (mejor cuanto más cerca de 0 por abajo). Un match genérico está en
    # el rango -20 a -1. Mapeamos: -20 → 60, 0 → 100, positivos (raros) → 50.
    def _bm25_to_score(bm25: float) -> int:
        if bm25 is None:
            return 50
        # Linear: cada -1 de bm25 son ~2 puntos hacia arriba, cap 100 / 40
        s = 100 + bm25 * 2
        return int(max(40, min(100, s)))

    out: list[SearchResult] = []
    for row in rows:
        s = _bm25_to_score(row.get("rank"))
        if s < _MIN_SCORE:
            continue
        # Bonus: exact match sobre name_normalized se garantiza como 100.
        # (bm25 puede darle 95 y colar tras un prefix match; nos aseguramos.)
        if row["name_normalized"] == q_norm:
            s = 100
        tag_list = [t for t in (row.get("tags") or "").split(",") if t]
        out.append(SearchResult(
            file_id=row["file_id"],
            filename=row["filename"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            folder_path=row.get("folder_path") or "",
            thumb_url=_thumb_url(row["file_id"]),
            download_url=_download_url(row["file_id"]),
            score=s,
            tags=tag_list,
            is_full_art=bool(row.get("is_full_art")),
            is_borderless=bool(row.get("is_borderless")),
            is_extended=bool(row.get("is_extended")),
            is_showcase=bool(row.get("is_showcase")),
            is_retro=bool(row.get("is_retro")),
            is_textless=bool(row.get("is_textless")),
            is_promo=bool(row.get("is_promo")),
            is_alt_art=bool(row.get("is_alt_art")),
            expansion_code=row.get("expansion_code"),
            collector_number=row.get("collector_number"),
            image_hash=row.get("image_hash"),
        ))

    # Reordenar por score final (exact matches primero, luego bm25 mapeado).
    out.sort(key=lambda r: (-r.score, r.filename))
    return out[:limit]


async def search(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    source_ids: list[int] | None = None,
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    expansion_code: str | None = None,
) -> list[SearchResult]:
    """Busca `query` en el índice y devuelve top-N por relevancia.

    Estrategia (Fase 2 · T6): si FTS5 está compilado en la SQLite del usuario,
    usamos ranking BM25 nativo con tokenizer unicode61 (asciifolding gratuito).
    Si no (build antigua de Windows sin FTS5), caemos al pipeline LIKE
    tri-fase que ya teníamos, sin pérdida de funcionalidad.

    Ambos paths respetan los mismos filtros:
      - ``tags_include``: solo artes que TIENEN todos los tags indicados.
      - ``tags_exclude``: descarta artes que tengan CUALQUIERA de estos.
      - ``expansion_code``: filtra por set canónico `[SET NUM]`.
      - ``source_ids``: limita a un subconjunto de drives.

    Devuelve `SearchResult` ordenados por score descendente. El score está en
    rango 0-100 tanto en modo FTS5 (bm25 remapeado) como en modo LIKE
    (rapidfuzz), aunque los números no son directamente comparables entre
    modos — solo importa el orden relativo dentro de una misma llamada.
    """
    q_norm = normalize_filename(query)
    if not q_norm:
        return []

    # FTS5 first — si está disponible, es 10-100x más rápido en índices grandes.
    if await _fts5_available(db):
        try:
            return await _search_fts5(
                db, q_norm, limit, source_ids, tags_include, tags_exclude, expansion_code,
            )
        except Exception as e:  # noqa: BLE001
            # Si FTS5 falla por cualquier motivo (query mal parseada, índice
            # corrupto), caemos al modo LIKE. Loguearemos como warning para
            # que se investigue pero el usuario NO ve un error.
            log.warning("FTS5 search failed, falling back to LIKE: %s", e)

    # --- Fallback: pipeline LIKE original (Fase 1) ---
    base = select(IndexedArt, ArtSource.name).join(
        ArtSource, ArtSource.id == IndexedArt.source_id
    )
    if source_ids:
        base = base.where(IndexedArt.source_id.in_(source_ids))

    # Filtro por set canónico. La columna ya está indexada.
    if expansion_code:
        base = base.where(IndexedArt.expansion_code == expansion_code.lower())

    # Traducción tag → columna. Si el cliente pasa un tag desconocido, lo
    # ignoramos (no forzamos error para tolerar clientes desactualizados).
    _TAG_TO_COLUMN = {
        "full_art":   IndexedArt.is_full_art,
        "borderless": IndexedArt.is_borderless,
        "extended":   IndexedArt.is_extended,
        "showcase":   IndexedArt.is_showcase,
        "retro":      IndexedArt.is_retro,
        "textless":   IndexedArt.is_textless,
        "promo":      IndexedArt.is_promo,
        "alt_art":    IndexedArt.is_alt_art,
    }
    if tags_include:
        for t in tags_include:
            col = _TAG_TO_COLUMN.get(t)
            if col is not None:
                base = base.where(col.is_(True))
    if tags_exclude:
        for t in tags_exclude:
            col = _TAG_TO_COLUMN.get(t)
            if col is not None:
                base = base.where(col.is_(False))

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
        tag_list = [t for t in (art.tags or "").split(",") if t]
        scored.append((s, SearchResult(
            file_id=art.file_id,
            filename=art.filename,
            source_id=art.source_id,
            source_name=source_name,
            folder_path=art.folder_path,
            thumb_url=_thumb_url(art.file_id),
            download_url=_download_url(art.file_id),
            score=s,
            tags=tag_list,
            is_full_art=bool(art.is_full_art),
            is_borderless=bool(art.is_borderless),
            is_extended=bool(art.is_extended),
            is_showcase=bool(art.is_showcase),
            is_retro=bool(art.is_retro),
            is_textless=bool(art.is_textless),
            is_promo=bool(art.is_promo),
            is_alt_art=bool(art.is_alt_art),
            expansion_code=art.expansion_code,
            collector_number=art.collector_number,
            image_hash=art.image_hash,
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
        "fts5_available": await _fts5_available(db),
    }
