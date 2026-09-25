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
    score: int
    tags: list[str]
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


def _thumb_url(file_id: str, size: int = 300) -> str:
    """URL de thumbnail servida por Google. `sz=w300` funciona hasta ~1024."""
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w{size}"


def _download_url(file_id: str) -> str:
    """URL de descarga directa (funciona para archivos < ~100MB sin token extra)."""
    return f"https://drive.google.com/uc?id={file_id}&export=download"


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
        elif noise_ratio <= 0.25:
            return 90
        elif noise_ratio <= 0.5:
            return 60
        elif noise_ratio <= 1.0:
            return 40
        else:
            return 0

    if q_set.issubset(n_set):
        if name_norm.startswith(query_norm + " ") or name_norm == query_norm:
            base = _by_noise_ratio(len(n_tokens) - len(q_tokens))
            return min(base + 5, 100)
        return _by_noise_ratio(len(n_set) - len(q_set))

    r = int(fuzz.ratio(query_norm, name_norm))
    if r >= 85:
        return r
    return 0


_MIN_SCORE = 55
_MAX_CANDIDATES = 5000


@dataclass
class SearchPage:
    """Una página de resultados y el total de coincidencias.

    ``capped`` indica que había más candidatos que ``_MAX_CANDIDATES`` y el
    total es un mínimo, no la cifra exacta.
    """
    results: list[SearchResult]
    total: int
    capped: bool = False


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
    except Exception:
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
    tokens = [t for t in query.split() if t]
    if not tokens:
        return ""
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
    source_ids: list[int] | None,
    tags_include: list[str] | None,
    tags_exclude: list[str] | None,
    expansion_code: str | None,
) -> SearchPage:
    """Búsqueda vía tabla virtual FTS5 con ranking BM25.

    JOIN entre `indexed_art_fts` (que devuelve rowid ordenado por bm25) y la
    tabla real `indexed_art` para traer todas las columnas + `art_sources`
    para el nombre del source.

    Los filtros por tag / source / expansion se aplican en el WHERE del JOIN,
    no en el MATCH — SQLite es eficiente combinando ambos (usa el índice
    invertido para la primera cardinalidad, luego filtra).

    Devuelve TODAS las coincidencias (hasta `_MAX_CANDIDATES`) puntuadas con
    `_score_match`, igual que el modo LIKE; bm25 solo desempata.
    """
    fts_query = _fts_escape(q_norm)
    if not fts_query:
        return SearchPage(results=[], total=0)

    where_parts = ["indexed_art_fts MATCH :match"]
    params: dict = {"match": fts_query, "limit": _MAX_CANDIDATES + 1}

    if source_ids:
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
        return SearchPage(results=[], total=0)

    capped = len(rows) > _MAX_CANDIDATES
    rows = rows[:_MAX_CANDIDATES]
    out: list[SearchResult] = []
    for row in rows:
        s = _score_match(q_norm, row["name_normalized"])
        if s < _MIN_SCORE:
            continue
        tag_list = [t for t in (row.get("tags") or "").split(",") if t]
        out.append(SearchResult(
            file_id=row["file_id"],
            filename=row["filename"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            folder_path=row.get("folder_path") or "",
            thumb_url=row.get("thumb_url") or _thumb_url(row["file_id"]),
            download_url=row.get("download_url") or _download_url(row["file_id"]),
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

    out.sort(key=lambda r: -r.score)
    return SearchPage(results=out, total=len(out), capped=capped)


async def search(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    source_ids: list[int] | None = None,
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    expansion_code: str | None = None,
) -> list[SearchResult]:
    """Top-``limit`` resultados para ``query``. Ver :func:`search_page`."""
    page = await search_page(
        db, query, limit=limit, offset=0, source_ids=source_ids,
        tags_include=tags_include, tags_exclude=tags_exclude,
        expansion_code=expansion_code,
    )
    return page.results


async def search_page(
    db: AsyncSession,
    query: str,
    limit: int = 20,
    offset: int = 0,
    source_ids: list[int] | None = None,
    tags_include: list[str] | None = None,
    tags_exclude: list[str] | None = None,
    expansion_code: str | None = None,
) -> SearchPage:
    """Una página de resultados ordenados por relevancia, y el total."""
    full = await _search_all(
        db, query, source_ids, tags_include, tags_exclude, expansion_code,
    )
    start = max(0, offset)
    return SearchPage(
        results=full.results[start : start + max(0, limit)],
        total=full.total,
        capped=full.capped,
    )


async def _search_all(
    db: AsyncSession,
    query: str,
    source_ids: list[int] | None,
    tags_include: list[str] | None,
    tags_exclude: list[str] | None,
    expansion_code: str | None,
) -> SearchPage:
    """Busca `query` en el índice y devuelve TODAS las coincidencias ordenadas.

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
        return SearchPage(results=[], total=0)

    if await _fts5_available(db):
        try:
            page = await _search_fts5(
                db, q_norm, source_ids, tags_include, tags_exclude, expansion_code,
            )
            if page.total:
                return page
        except Exception as e:
            log.warning("FTS5 search failed, falling back to LIKE: %s", e)

    base = select(IndexedArt, ArtSource.name).join(
        ArtSource, ArtSource.id == IndexedArt.source_id
    )
    if source_ids:
        base = base.where(IndexedArt.source_id.in_(source_ids))

    if expansion_code:
        base = base.where(IndexedArt.expansion_code == expansion_code.lower())

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

    stmt = base.where(IndexedArt.name_normalized == q_norm).limit(_MAX_CANDIDATES + 1)
    rows = (await db.execute(stmt)).all()

    if len(rows) < 20:
        stmt = base.where(
            IndexedArt.name_normalized.like(f"{q_norm}%"),
            IndexedArt.name_normalized != q_norm,
        ).limit(_MAX_CANDIDATES + 1 - len(rows))
        rows += (await db.execute(stmt)).all()

    if len(rows) < 20:
        long_tokens = [t for t in q_norm.split() if len(t) >= 3]
        if long_tokens:
            conds = [IndexedArt.name_normalized.like(f"%{t}%") for t in long_tokens]
            stmt = base.where(
                or_(*conds),
                ~IndexedArt.name_normalized.like(f"{q_norm}%"),
                IndexedArt.name_normalized != q_norm,
            ).limit(_MAX_CANDIDATES + 1 - len(rows))
            rows += (await db.execute(stmt)).all()

    if not rows:
        return SearchPage(results=[], total=0)
    capped = len(rows) > _MAX_CANDIDATES
    rows = rows[:_MAX_CANDIDATES]

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
            thumb_url=art.thumb_url or _thumb_url(art.file_id),
            download_url=art.download_url or _download_url(art.file_id),
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
    results = [r for _, r in scored]
    return SearchPage(results=results, total=len(results), capped=capped)


async def list_cardbacks(
    db: AsyncSession,
    limit: int = 100,
    source_ids: list[int] | None = None,
) -> list[SearchResult]:
    """Devuelve todos los artes indexados con ``card_type = 'CARDBACK'``.

    No requiere query de búsqueda: simplemente filtra por ``card_type`` que
    el indexador asigna basándose en la carpeta contenedora (``Cardbacks/``),
    replicando la lógica de MPC Autofill. Esto excluye caras traseras de DFC
    marcadas con ``(B)`` en el nombre (que solo tienen el tag ``back`` pero
    ``card_type = 'CARD'``).

    Ordena por source_name y luego filename para dar un listado estable.
    """
    from sqlalchemy import text as sa_text

    where_parts = ["ia.card_type = 'CARDBACK'"]
    params: dict = {"limit": limit}

    if source_ids:
        ids = ",".join(str(int(x)) for x in source_ids)
        where_parts.append(f"ia.source_id IN ({ids})")

    where_sql = " AND ".join(where_parts)

    sql = f"""
        SELECT ia.*, s.name AS source_name
        FROM indexed_art AS ia
        JOIN art_sources AS s ON s.id = ia.source_id
        WHERE {where_sql}
        ORDER BY s.name, ia.filename
        LIMIT :limit
    """
    result = await db.execute(sa_text(sql), params)
    rows = result.mappings().all()

    out: list[SearchResult] = []
    for row in rows:
        tag_list = [t for t in (row.get("tags") or "").split(",") if t]
        out.append(SearchResult(
            file_id=row["file_id"],
            filename=row["filename"],
            source_id=row["source_id"],
            source_name=row.get("source_name", ""),
            folder_path=row.get("folder_path") or "",
            thumb_url=row.get("thumb_url") or _thumb_url(row["file_id"]),
            download_url=row.get("download_url") or _download_url(row["file_id"]),
            score=100,
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
    return out


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
