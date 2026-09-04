"""Recomendador de artes por artista canónico (Fase 3 · T11).

Motivación
----------
Cuando el usuario elige un arte de artist X para una carta, es muy probable
que quiera un look consistente para el resto del mazo. Este servicio dada
una lista de cartas + un nombre de artista, devuelve qué cartas tienen al
menos una impresión de ese artista.

Cómo funciona
-------------
1. Recibimos ``oracle_ids`` (uno por carta única del mazo) + ``artist``.
2. **Extras · T11**: consulta primero al cache local `oracle_artists`. Si
   todos los oracles ya están cacheados con TTL vigente, respuesta en
   <100ms sin red. Para los oracles no cacheados o stale, cae a Scryfall
   (`prints_by_oracle_id`) y persiste los resultados.
3. Filtramos las que Scryfall marca como del ``artist`` pedido (matching
   case-insensitive con asciifolding — "Yeong-Hao Han" → "yeong hao han").
4. Devolvemos por carta la mejor impresión (regular art > full art > alt;
   priorizamos también rarity y released_at para preferir versiones más
   recientes en la duda).

Rendimiento
-----------
- Primera vez: N oracle_ids sin cache = N llamadas ``prints_by_oracle_id``.
  Cap a `MAX_ORACLES_PER_REQUEST` para tiempo bloqueante razonable (~10s).
- Segunda vez (o mazo con overlap): O(1) por oracle desde cache local.
"""
from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import OracleArtistCache

log = logging.getLogger(__name__)

# Máximo de oracles a procesar en una request bloqueante.
MAX_ORACLES_PER_REQUEST = 25

# Concurrencia de fetches a Scryfall. El propio ScryfallClient ya limita a
# ~10 req/s vía rate limiter; pedir varias en paralelo permite solapar la
# latencia de red con la cadencia sin pasarnos.
_FETCH_CONCURRENCY = 8

# TTL del cache local. Nuevas printings salen con cada set (~cada 3 meses),
# 7 días es suficientemente fresco para uso normal.
CACHE_TTL = timedelta(days=7)


def _fold(name: str) -> str:
    """Asciifolding + lowercase igual que el indexer, para matching robusto
    de artistas con acentos ("Yeong Hao Han" vs "Yeong-Hao Han").
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    # Colapsamos guiones a espacios (algunos artistas aparecen así en Scryfall).
    n = n.replace("-", " ")
    # Multiple espacios → uno
    return " ".join(n.lower().split())


@dataclass
class ArtistMatch:
    """Una impresión de una carta hecha por el artista buscado."""
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None
    image_normal: str | None
    released_at: str | None
    is_full_art: bool
    is_promo: bool


@dataclass
class RecommendResult:
    """Salida del recomendador.

    ``matched`` lista las cartas donde encontramos al menos una impresión del
    artista. Cada entrada trae la MEJOR impresión (regular > full art > promo;
    más reciente en desempate).

    ``unmatched`` son los oracle_ids que sí procesamos pero no tenían nada
    del artista. Sirve para que el frontend enseñe "cover 12 de 25 cartas".

    ``skipped`` cuando hay > MAX_ORACLES_PER_REQUEST; el resto se pospone.
    """
    artist_query: str
    matched: list[ArtistMatch]
    unmatched: list[str]
    skipped: list[str]


def _pick_best_printing(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """De varias impresiones del mismo artist para la misma carta, elige la
    más 'estándar' — evita promos y proxies raros salvo que sea todo lo que hay.

    Criterio (menor score = mejor):
      1. NO promo (base cards > promos)
      2. NO full_art
      3. released_at descendente (más reciente primero para look actual)
    """
    if not candidates:
        return None

    def _score(c: dict[str, Any]) -> tuple[int, int, str]:
        return (
            1 if c.get("promo") else 0,
            1 if c.get("full_art") else 0,
            # Truco: released_at descendente → invertimos con negación
            # ordenando por string (2026 > 2020) y luego negando el
            # comparador; usamos min() sobre ese tuple positivo.
            # Simplificamos: prefijo por reversed date usando negation.
            # Para sort ascendente con lo más reciente primero, invertimos:
            "9" if not c.get("released_at") else c["released_at"],
        )

    ranked = sorted(candidates, key=_score)
    # Los más recientes primero deshaciendo la clave: filtramos entre los
    # mejores por (promo, full_art) y ordenamos por released_at descendente.
    best_priority = _score(ranked[0])[:2]
    top_bucket = [c for c in candidates
                  if (1 if c.get("promo") else 0, 1 if c.get("full_art") else 0) == best_priority]
    top_bucket.sort(key=lambda c: c.get("released_at") or "", reverse=True)
    return top_bucket[0]


def _to_match(oracle_id: str, printing: dict[str, Any]) -> ArtistMatch:
    imgs = printing.get("image_uris") or {}
    # Para DFC/MDFC coger el frente si existen faces
    if not imgs and printing.get("card_faces"):
        imgs = printing["card_faces"][0].get("image_uris", {}) or {}
    return ArtistMatch(
        oracle_id=oracle_id,
        card_name=printing.get("name", ""),
        scryfall_id=printing.get("id", ""),
        set_code=(printing.get("set") or "").lower(),
        set_name=printing.get("set_name") or "",
        collector_number=printing.get("collector_number") or "",
        artist=printing.get("artist") or "",
        image_small=imgs.get("small"),
        image_normal=imgs.get("normal"),
        released_at=printing.get("released_at"),
        is_full_art=bool(printing.get("full_art")),
        is_promo=bool(printing.get("promo")),
    )


async def _lookup_cache(
    db: AsyncSession, oracle_ids: list[str], artist_folded: str,
) -> tuple[set[str], list[str]]:
    """Consulta el cache local para saber qué oracles ya sabemos que TIENEN
    (o no) el artista pedido.

    Devuelve ``(oracles_with_artist, oracles_needing_scryfall)``:
      - ``oracles_with_artist``: subset de oracle_ids que sabemos que tienen
        una printing del artista, sin necesidad de red.
      - ``oracles_needing_scryfall``: oracle_ids cuyos datos NO están en el
        cache o están stale (TTL expirado). Requieren fetch.

    Nota: si tenemos rows del oracle en cache PERO ninguna coincide con el
    artist, consideramos que "sabemos que NO tiene" y NO lo devolvemos en
    ``oracles_needing_scryfall``. Esto es el equivalente al lookup negativo
    cacheado — evita re-preguntar por oracles que ya vimos.
    """
    if not oracle_ids:
        return set(), []
    stale_cutoff = datetime.now(timezone.utc) - CACHE_TTL

    # Traer todas las filas del cache para estos oracles.
    rows = (await db.execute(
        select(OracleArtistCache.oracle_id,
               OracleArtistCache.artist_folded,
               OracleArtistCache.fetched_at)
        .where(OracleArtistCache.oracle_id.in_(oracle_ids))
    )).all()

    # Estructura: {oracle_id: [(artist_folded, fetched_at), …]}
    by_oracle: dict[str, list[tuple[str, datetime]]] = {}
    for oid, af, fetched in rows:
        by_oracle.setdefault(oid, []).append((af, fetched))

    with_artist: set[str] = set()
    need_fetch: list[str] = []
    for oid in oracle_ids:
        cached = by_oracle.get(oid)
        if not cached:
            need_fetch.append(oid)
            continue
        # ¿Alguna fila está stale? Si sí, refresh completo.
        # Nota: `fetched_at` puede ser naive (SQLite lo guarda sin tz).
        any_stale = any(
            (f.replace(tzinfo=timezone.utc) if f.tzinfo is None else f) < stale_cutoff
            for _, f in cached
        )
        if any_stale:
            need_fetch.append(oid)
            continue
        # Cached y fresco: ¿tiene el artist pedido?
        if any(af == artist_folded for af, _ in cached):
            with_artist.add(oid)
        # Si no matchea, sabemos que NO tiene y no vamos a Scryfall.
    return with_artist, need_fetch


async def _persist_cache(
    db: AsyncSession, oracle_id: str, prints: list[dict[str, Any]],
) -> None:
    """Persiste en `OracleArtistCache` los artistas encontrados para
    ``oracle_id``. Reemplaza cualquier fila previa del mismo oracle (evita
    duplicados / stale data mezclada). Idempotente.

    Extrae artist de card_faces también para cubrir DFC/split cards.
    """
    # Recolectar todos los artist_folded distintos que aparecen para este oracle.
    artists: set[tuple[str, str]] = set()  # (folded, display)
    for p in prints:
        candidates: list[str] = []
        if p.get("artist"):
            candidates.append(p["artist"])
        for face in (p.get("card_faces") or []):
            if face.get("artist"):
                candidates.append(face["artist"])
        for a in candidates:
            folded = _fold(a)
            if folded:
                artists.add((folded, a))

    # Purga la entrada previa (o entradas huérfanas)
    await db.execute(
        delete(OracleArtistCache).where(OracleArtistCache.oracle_id == oracle_id)
    )
    now = datetime.now(timezone.utc)
    for folded, display in artists:
        db.add(OracleArtistCache(
            oracle_id=oracle_id, artist_folded=folded,
            artist_display=display, fetched_at=now,
        ))
    # Si no hay artistas (raro), insertamos una fila sentinela con
    # artist_folded="" para no re-fetchear infinitamente.
    if not artists:
        db.add(OracleArtistCache(
            oracle_id=oracle_id, artist_folded="",
            artist_display="", fetched_at=now,
        ))


async def recommend_by_artist(
    scryfall: ScryfallClient,
    oracle_ids: list[str],
    artist: str,
    db: AsyncSession | None = None,
) -> "RecommendResult":
    """Para cada oracle_id, busca impresiones por ``artist`` y devuelve la
    mejor cover encontrada.

    - Deduplica oracle_ids (mazos con múltiples copias generan uno solo).
    - Cache-aware (Extras · T11): si ``db`` se pasa, usa la tabla
      ``oracle_artists`` para saltar fetches redundantes; si no, comportamiento
      original con N calls a Scryfall.
    - Máximo ``MAX_ORACLES_PER_REQUEST`` oracles no-cacheados por llamada;
      el resto en `skipped`.
    """
    artist_folded = _fold(artist)
    if not artist_folded:
        return RecommendResult(artist_query=artist, matched=[], unmatched=[], skipped=[])

    unique_oracles = list(dict.fromkeys(o for o in oracle_ids if o))

    # Fase A: consulta al cache local (si tenemos db).
    cached_with_artist: set[str] = set()
    to_fetch = unique_oracles
    if db is not None:
        cached_with_artist, to_fetch = await _lookup_cache(
            db, unique_oracles, artist_folded,
        )

    # Fase B: fetch a Scryfall SOLO para los no cacheados o stale.
    fetch_now = to_fetch[:MAX_ORACLES_PER_REQUEST]
    skipped_oracles = to_fetch[MAX_ORACLES_PER_REQUEST:]

    fetched_prints = await _fetch_prints_parallel(scryfall, fetch_now)

    # Persistir en cache para próximas queries. Escritura secuencial: mismos
    # motivos que en art_cache (AsyncSession no soporta uso concurrente).
    if db is not None:
        for oid, prints in fetched_prints.items():
            try:
                await _persist_cache(db, oid, prints)
            except Exception as e:  # noqa: BLE001
                log.warning("persist_cache(%s) falló: %s", oid, e)
        await db.commit()

    # Fase C: construir la respuesta unificando cache + fetch.
    matched: list[ArtistMatch] = []
    unmatched: list[str] = []

    # Los oracles cacheados como "sí tiene este artist" solo contienen el
    # mapping — necesitamos las impresiones concretas para devolver la mejor.
    # Refresh en paralelo, respetando el cap global.
    cached_needing_prints = cached_with_artist - set(fetched_prints.keys())
    extra_fetch = list(cached_needing_prints)[: MAX_ORACLES_PER_REQUEST - len(fetch_now)]
    if extra_fetch:
        fetched_prints.update(await _fetch_prints_parallel(scryfall, extra_fetch))
    skipped_oracles += list(cached_needing_prints - set(extra_fetch))

    for oid in unique_oracles:
        prints = fetched_prints.get(oid, [])
        if not prints:
            if oid in cached_with_artist and oid in skipped_oracles:
                # No mostramos como unmatched — se explicita en `skipped`.
                continue
            if oid in cached_with_artist:
                # El cache dice que sí tiene pero fetch falló. Marcamos unmatched.
                unmatched.append(oid)
                continue
            if oid in fetch_now:
                # Fetch OK pero sin printings del artist
                unmatched.append(oid)
            # Los otros no requieren nada (cache dice que NO tiene)
            continue
        # Filtrar por artist folded
        candidates: list[dict[str, Any]] = []
        for p in prints:
            artist_candidates = [p.get("artist") or ""]
            for face in (p.get("card_faces") or []):
                if face.get("artist"):
                    artist_candidates.append(face["artist"])
            if any(artist_folded in _fold(a) for a in artist_candidates):
                candidates.append(p)
        best = _pick_best_printing(candidates)
        if best:
            matched.append(_to_match(oid, best))
        elif oid in fetch_now:
            unmatched.append(oid)

    return RecommendResult(
        artist_query=artist,
        matched=matched,
        unmatched=unmatched,
        skipped=skipped_oracles,
    )


# ---------------------------------------------------------------------------
# Recomendador por set / style (Extras · F3/T11)
# ---------------------------------------------------------------------------

@dataclass
class StyleMatch:
    """Match encontrado por criterio de estilo (set o borderless/showcase/etc).

    Estructura similar a ArtistMatch para simplificar el consumo desde la UI.
    """
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None
    image_normal: str | None
    released_at: str | None
    matched_criteria: list[str]  # ["set:mom", "borderless", "showcase"]


@dataclass
class StyleRecommendResult:
    query: dict[str, Any]
    matched: list[StyleMatch]
    unmatched: list[str]
    skipped: list[str]


def _printing_matches_style(
    printing: dict[str, Any],
    set_code: str | None,
    borderless: bool,
    showcase: bool,
    extended: bool,
    full_art: bool,
) -> list[str]:
    """Devuelve la lista de criterios que ``printing`` cumple. Vacía si ninguno.

    Un printing "matchea" cuando cumple TODOS los criterios especificados
    (AND). Devolvemos la lista de labels para trazabilidad ("aquí matchean
    set:MOM + borderless").
    """
    labels: list[str] = []
    if set_code:
        if (printing.get("set") or "").lower() != set_code.lower():
            return []
        labels.append(f"set:{set_code.lower()}")
    if borderless:
        if (printing.get("border_color") or "").lower() != "borderless":
            return []
        labels.append("borderless")
    if showcase:
        # Scryfall: `frame_effects` incluye "showcase" para cartas showcase.
        effects = printing.get("frame_effects") or []
        if "showcase" not in effects:
            return []
        labels.append("showcase")
    if extended:
        effects = printing.get("frame_effects") or []
        if "extendedart" not in effects:
            return []
        labels.append("extended")
    if full_art:
        if not printing.get("full_art"):
            return []
        labels.append("full_art")
    return labels


async def recommend_by_style(
    scryfall: ScryfallClient,
    oracle_ids: list[str],
    *,
    set_code: str | None = None,
    borderless: bool = False,
    showcase: bool = False,
    extended: bool = False,
    full_art: bool = False,
    db: AsyncSession | None = None,
) -> StyleRecommendResult:
    """Recomienda impresiones que cumplen criterios de estilo.

    Los criterios se combinan con AND: si pasas ``set_code="mom"`` +
    ``borderless=True``, solo devuelve printings que sean del set MOM y
    además borderless. Al menos UN criterio debe estar activo, si no,
    devolvemos vacío (no tendría sentido).

    Usa `prints_by_oracle_id` para cada oracle (misma limitación de
    performance que `recommend_by_artist`). El cache local aún no aplica
    aquí — cachear "printings de un oracle" es un TODO diferente porque
    las respuestas son grandes.
    """
    # Al menos un filtro activo
    if not any([set_code, borderless, showcase, extended, full_art]):
        return StyleRecommendResult(
            query={"set_code": set_code, "borderless": borderless,
                   "showcase": showcase, "extended": extended,
                   "full_art": full_art},
            matched=[], unmatched=[], skipped=[],
        )

    unique = list(dict.fromkeys(o for o in oracle_ids if o))
    to_fetch = unique[:MAX_ORACLES_PER_REQUEST]
    skipped = unique[MAX_ORACLES_PER_REQUEST:]

    matched: list[StyleMatch] = []
    unmatched: list[str] = []

    prints_by_oid = await _fetch_prints_parallel(scryfall, to_fetch)

    for oid in to_fetch:
        prints = prints_by_oid.get(oid, [])
        if not prints:
            unmatched.append(oid)
            continue

        best: tuple[dict[str, Any], list[str]] | None = None
        for p in prints:
            labels = _printing_matches_style(
                p, set_code, borderless, showcase, extended, full_art,
            )
            if labels:
                if best is None or (p.get("released_at") or "") > (best[0].get("released_at") or ""):
                    best = (p, labels)

        if best:
            p, labels = best
            imgs = p.get("image_uris") or {}
            if not imgs and p.get("card_faces"):
                imgs = p["card_faces"][0].get("image_uris", {}) or {}
            matched.append(StyleMatch(
                oracle_id=oid,
                card_name=p.get("name", ""),
                scryfall_id=p.get("id", ""),
                set_code=(p.get("set") or "").lower(),
                set_name=p.get("set_name") or "",
                collector_number=p.get("collector_number") or "",
                artist=p.get("artist") or "",
                image_small=imgs.get("small"),
                image_normal=imgs.get("normal"),
                released_at=p.get("released_at"),
                matched_criteria=labels,
            ))
        else:
            unmatched.append(oid)

    return StyleRecommendResult(
        query={"set_code": set_code, "borderless": borderless,
               "showcase": showcase, "extended": extended, "full_art": full_art},
        matched=matched, unmatched=unmatched, skipped=skipped,
    )


async def _fetch_prints_parallel(
    scryfall: ScryfallClient, oracle_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch de ``prints_by_oracle_id`` para varios oracles en paralelo.

    Acota concurrencia con un semáforo. El ``ScryfallClient`` mantiene su
    propio rate limit interno, así que este ``gather`` solapa la latencia
    de red respetando la cadencia global.
    """
    if not oracle_ids:
        return {}

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(oid: str) -> tuple[str, list[dict[str, Any]]]:
        async with semaphore:
            try:
                return oid, await scryfall.prints_by_oracle_id(oid)
            except Exception as e:  # noqa: BLE001
                log.warning("prints_by_oracle_id(%s) falló: %s", oid, e)
                return oid, []

    results = await asyncio.gather(*(_one(oid) for oid in oracle_ids))
    return dict(results)
