"""Perceptual hashing (pHash) para dedupe cross-drive.

Motivación
----------
Muchos drives comunitarios contienen las mismas imágenes (redistribuidas /
mirroreadas / copiadas). El usuario ve "Elesh Norn (Full Art).png" en 8
drives distintos, todos idénticos, saturando el picker. Un pHash sobre el
thumbnail permite detectarlas como equivalentes y agruparlas.

Diseño
------
- **Hash**: pHash de 64 bits (algoritmo DCT) → 16 chars hex en la BD.
- **Distancia**: hamming distance. ≤ 8 bits de diferencia se considera
  "misma imagen" (tolera JPEG re-encoding, escalado ≤2x, watermark leve).
- **Escala del cálculo**: descargar el thumbnail (~50KB) y decodificarlo.
  Para 100k artes = 5 GB de bandwidth, ~10 min con paralelismo.
- **Opt-in**: setting `phash.enabled` (default OFF). El usuario lo activa
  desde Ajustes cuando quiere pagar el coste.

Dependencias
------------
- ``Pillow`` para decodificar imágenes (ya recomendable como dep).
- ``imagehash`` (~2 KB, sin deps propias — usa Pillow y numpy).

Si alguna falta, el servicio degrada silenciosamente: `compute_phash()`
devuelve None y `enabled()` devuelve False. Nada rompe.

Uso
---
    if await phash.enabled(db):
        h = await phash.compute_for_url(art_cache_client, thumb_url)
        if h:
            art.image_hash = h

    # Búsqueda de similares:
    similar = await phash.find_similar(db, art.image_hash, threshold=8)
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import IndexedArt

log = logging.getLogger(__name__)

# Descargas de thumbnails simultáneas durante el retrofit. Mismo criterio que
# `ArtCache._DOWNLOAD_CONCURRENCY`.
_RETROFIT_CONCURRENCY = 8

# Cada cuántos artes se hace commit. Acota lo que se pierde si el proceso
# muere a mitad del job.
_RETROFIT_BATCH = 40

# Detección lazy de las dependencias opcionales. Si no están, la función
# `is_available()` devuelve False y el resto del módulo no se usa.
_pil = None
_imagehash = None
_deps_checked = False


def _check_deps() -> bool:
    """Detecta Pillow + imagehash una sola vez (cached en globals)."""
    global _pil, _imagehash, _deps_checked
    if _deps_checked:
        return _pil is not None and _imagehash is not None
    _deps_checked = True
    try:
        import imagehash as _ih_module  # type: ignore
        from PIL import Image as _pil_module  # type: ignore
        _pil = _pil_module
        _imagehash = _ih_module
        return True
    except ImportError as e:
        log.info(
            "phash: dependencias opcionales (Pillow, imagehash) no disponibles: %s. "
            "La feature de dedupe cross-drive queda desactivada. Instala con "
            "`pip install Pillow imagehash` para activarla.", e
        )
        return False


def is_available() -> bool:
    """True si Pillow + imagehash están instalados. Sin ellos, el servicio
    completo es un no-op."""
    return _check_deps()


async def enabled(db: AsyncSession) -> bool:
    """True si el setting `phash.enabled` está en `true` Y las dependencias
    están instaladas. Los callers usan esto para saber si deben calcular
    pHash al indexar.
    """
    if not is_available():
        return False
    from mpc_forge.services import settings as settings_service
    settings = await settings_service.get_all(db)
    val = settings.get("phash.enabled", False)
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}


def compute_from_bytes(data: bytes) -> str | None:
    """Calcula pHash desde bytes de imagen. Devuelve 16 chars hex o None si
    falla el decode. NO hace I/O — solo decode + hash, apto para offloadear
    a un thread pool si se quiere paralelizar CPU-bound.
    """
    if not is_available():
        return None
    try:
        img = _pil.open(io.BytesIO(data))
        # imagehash requiere modo L o RGB — conversión defensiva.
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        h = _imagehash.phash(img)
        # imagehash.__str__ devuelve hex de 16 chars para pHash de 64 bits.
        return str(h)
    except Exception as e:
        log.debug("compute_from_bytes falló: %s", e)
        return None


async def compute_from_bytes_async(data: bytes) -> str | None:
    """``compute_from_bytes`` fuera del event loop.

    El decode con Pillow y la DCT del pHash son CPU pura: unos 50-150 ms por
    imagen. Llamándolo directamente desde una corrutina, un retrofit de 500
    artes congela el servidor entero durante ese rato — la UI deja de
    responder y hasta el polling de progreso se atasca. ``to_thread`` lo saca
    del hilo del event loop; Pillow libera el GIL en el decode, así que además
    escala de verdad.
    """
    if not is_available():
        return None
    return await asyncio.to_thread(compute_from_bytes, data)


async def compute_from_url(client: Any, url: str, timeout: float = 15.0) -> str | None:
    """Descarga el thumbnail (con el httpx.AsyncClient del caller) y calcula
    su pHash. Devuelve el hash o None si falla la descarga.

    El caller pasa su propio `client` para reusar conexiones y respetar los
    settings SSL de la app. Normalmente es `app.state.art_cache._client` o un
    httpx.AsyncClient dedicado.
    """
    if not is_available():
        return None
    try:
        resp = await client.get(url, timeout=timeout)
        if resp.status_code != 200:
            return None
        return await compute_from_bytes_async(resp.content)
    except Exception as e:
        log.debug("compute_from_url(%s) falló: %s", url, e)
        return None


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Distancia de Hamming entre dos hashes hex de 16 chars.

    Comparación bit-a-bit sobre los 64 bits del pHash. Retorna -1 si alguno
    de los hashes es inválido (no 16 chars hex).
    """
    if not hash_a or not hash_b or len(hash_a) != 16 or len(hash_b) != 16:
        return -1
    try:
        a = int(hash_a, 16)
        b = int(hash_b, 16)
    except ValueError:
        return -1
    return bin(a ^ b).count("1")


async def find_similar(
    db: AsyncSession,
    reference_hash: str,
    threshold: int = 8,
    exclude_file_id: str | None = None,
    limit: int = 50,
) -> list[IndexedArt]:
    """Encuentra artes con hamming distance ≤ threshold al ``reference_hash``.

    Estrategia: SQLite no tiene función hamming nativa. Traemos todos los
    hashes no-nulos y filtramos en memoria. Para índices grandes (>100k
    artes con hash), esto es O(N) pero N está capado por el usuario
    (Pillow es lento, típicamente <10k artes tienen hash calculado).

    ``exclude_file_id``: para excluir el propio arte de la búsqueda cuando
    llamas ``find_similar_to(art)``.
    """
    if not reference_hash:
        return []

    # Dos fases a propósito.
    #
    # Fase 1: traer SOLO (id, hash). Antes se hidrataban objetos IndexedArt
    # completos de todas las filas con hash para descartar el 99% acto
    # seguido: con 100k artes eso son 100k instancias ORM construidas y
    # tiradas, cientos de MB de pico y el event loop bloqueado mientras dura.
    # Una fila de dos columnas es una tupla ligera.
    #
    # Fase 2: hidratar únicamente los ganadores (`limit`, por defecto 50).
    rows = (await db.execute(
        select(IndexedArt.id, IndexedArt.image_hash, IndexedArt.file_id)
        .where(IndexedArt.image_hash.is_not(None))
    )).all()

    scored: list[tuple[int, int]] = []   # (distancia, id)
    for art_id, image_hash, file_id in rows:
        if exclude_file_id and file_id == exclude_file_id:
            continue
        d = hamming_distance(reference_hash, image_hash or "")
        if 0 <= d <= threshold:
            scored.append((d, art_id))

    if not scored:
        return []
    scored.sort(key=lambda x: x[0])
    winner_ids = [art_id for _, art_id in scored[:limit]]

    found = (await db.scalars(
        select(IndexedArt).where(IndexedArt.id.in_(winner_ids))
    )).all()
    # El IN no conserva el orden; lo reponemos según la distancia calculada.
    by_id = {a.id: a for a in found}
    return [by_id[i] for i in winner_ids if i in by_id]


async def compute_missing_for_source(
    db: AsyncSession,
    client: Any,
    source_id: int,
    limit: int = 500,
    thumb_url_fn=None,
) -> dict[str, int]:
    """Job de retrofit: calcula pHash para los primeros ``limit`` artes de un
    source que aún no lo tengan. Diseñado para llamarse en background /
    a demanda desde un botón "Calcular hashes" en Ajustes.

    ``thumb_url_fn``: callable ``(art) -> str`` que construye la URL del
    thumbnail para un arte. Si es None, se usa el helper interno que
    dispatcha al source_type.

    Devuelve stats ``{"computed": N, "failed": M, "skipped": K}``.
    """
    if not is_available():
        return {"computed": 0, "failed": 0, "skipped": 0, "error": "deps missing"}

    thumb_url_fn = thumb_url_fn or _default_thumb_url

    rows = (await db.scalars(
        select(IndexedArt)
        .where(
            IndexedArt.source_id == source_id,
            IndexedArt.image_hash.is_(None),
        )
        .limit(limit)
    )).all()

    stats = {"computed": 0, "failed": 0, "skipped": 0}

    # Antes esto era un `for` con un `await` de descarga dentro: una imagen
    # detrás de otra, sin solapar nada. Con ~200 ms de ida y vuelta por
    # thumbnail, 500 artes eran casi dos minutos de los cuales el 95% era
    # esperar a la red.
    #
    # Un semáforo permite tener varias descargas en vuelo respetando un tope.
    # El valor es el mismo que usa `ArtCache._DOWNLOAD_CONCURRENCY`: suficiente
    # para saturar el ancho de banda sin parecer un scraper agresivo.
    pending = [(art, thumb_url_fn(art)) for art in rows]
    stats["skipped"] = sum(1 for _, url in pending if not url)
    pending = [(art, url) for art, url in pending if url]

    semaphore = asyncio.Semaphore(_RETROFIT_CONCURRENCY)

    async def _one(art, url) -> tuple[Any, str | None]:
        async with semaphore:
            return art, await compute_from_url(client, url)

    # Se procesa en lotes para poder ir commiteando: si la conexión se cae a
    # la mitad, el trabajo ya hecho queda guardado.
    for batch_start in range(0, len(pending), _RETROFIT_BATCH):
        batch = pending[batch_start : batch_start + _RETROFIT_BATCH]
        results = await asyncio.gather(
            *(_one(art, url) for art, url in batch),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                # `compute_from_url` ya traga sus propios errores; si algo
                # llega hasta aquí es inesperado, pero no debe abortar el job
                # entero ni perder los hashes ya calculados.
                log.debug("Cálculo de pHash falló con excepción: %s", result)
                stats["failed"] += 1
                continue
            art, h = result
            if h:
                art.image_hash = h
                stats["computed"] += 1
            else:
                stats["failed"] += 1
        await db.commit()

    await db.commit()
    return stats


def _default_thumb_url(art: IndexedArt, source: Any | None = None) -> str:
    """URL de thumbnail por defecto para un arte indexado.

    Extras · F2/T8: dispatch por source_type usando el registry
    `services.source_types`. Si ``source`` viene pre-cargado (recomendado
    para batch), evitamos la query extra a la BD por arte. Si no, caemos
    al patrón gdrive (comportamiento previo, seguro por defecto).

    Prioridad:
      1. ``art.thumb_url`` si está poblada (Extras · T7).
      2. Dispatch por ``source.source_type`` vía registry.
      3. Fallback: patrón Google Drive.
    """
    # (1) URL directa cacheada
    if getattr(art, "thumb_url", None):
        return art.thumb_url

    # (2) Dispatch por source_type
    if source is not None:
        try:
            from mpc_forge.services.source_types import resolve as _resolve_type
            type_cls = _resolve_type(source.source_type)
            if type_cls:
                return type_cls.thumbnail_url(source, art.file_id)
        except Exception as e:
            log.debug("_default_thumb_url dispatch falló, cayendo a gdrive: %s", e)

    # (3) Fallback histórico
    return f"https://drive.google.com/thumbnail?id={art.file_id}&sz=w400"
