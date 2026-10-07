from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import IndexedArt

log = logging.getLogger(__name__)

_RETROFIT_CONCURRENCY = 8

_RETROFIT_BATCH = 40

_pil = None
_imagehash = None
_deps_checked = False


def _check_deps() -> bool:
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
            "`pip install Pillow imagehash` para activarla.",
            e,
        )
        return False


def is_available() -> bool:
    return _check_deps()


async def enabled(db: AsyncSession) -> bool:
    if not is_available():
        return False
    from mpc_forge.services import settings as settings_service

    settings = await settings_service.get_all(db)
    val = settings.get("phash.enabled", False)
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}


def compute_from_bytes(data: bytes) -> str | None:
    if not is_available():
        return None
    try:
        img = _pil.open(io.BytesIO(data))
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        h = _imagehash.phash(img)
        return str(h)
    except Exception as e:
        log.debug("compute_from_bytes falló: %s", e)
        return None


async def compute_from_bytes_async(data: bytes) -> str | None:
    if not is_available():
        return None
    return await asyncio.to_thread(compute_from_bytes, data)


async def compute_from_url(client: Any, url: str, timeout: float = 15.0) -> str | None:
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
    if not reference_hash:
        return []

    rows = (
        await db.execute(
            select(IndexedArt.id, IndexedArt.image_hash, IndexedArt.file_id).where(
                IndexedArt.image_hash.is_not(None)
            )
        )
    ).all()

    scored: list[tuple[int, int]] = []
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

    found = (await db.scalars(select(IndexedArt).where(IndexedArt.id.in_(winner_ids)))).all()
    by_id = {a.id: a for a in found}
    return [by_id[i] for i in winner_ids if i in by_id]


async def compute_missing_for_source(
    db: AsyncSession,
    client: Any,
    source_id: int,
    limit: int = 500,
    thumb_url_fn=None,
) -> dict[str, int]:
    if not is_available():
        return {"computed": 0, "failed": 0, "skipped": 0, "error": "deps missing"}

    thumb_url_fn = thumb_url_fn or _default_thumb_url

    rows = (
        await db.scalars(
            select(IndexedArt)
            .where(
                IndexedArt.source_id == source_id,
                IndexedArt.image_hash.is_(None),
            )
            .limit(limit)
        )
    ).all()

    stats = {"computed": 0, "failed": 0, "skipped": 0}

    pending = [(art, thumb_url_fn(art)) for art in rows]
    stats["skipped"] = sum(1 for _, url in pending if not url)
    pending = [(art, url) for art, url in pending if url]

    semaphore = asyncio.Semaphore(_RETROFIT_CONCURRENCY)

    async def _one(art, url) -> tuple[Any, str | None]:
        async with semaphore:
            return art, await compute_from_url(client, url)

    for batch_start in range(0, len(pending), _RETROFIT_BATCH):
        batch = pending[batch_start : batch_start + _RETROFIT_BATCH]
        results = await asyncio.gather(
            *(_one(art, url) for art, url in batch),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
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
    if getattr(art, "thumb_url", None):
        return art.thumb_url

    if source is not None:
        try:
            from mpc_forge.services.source_types import resolve as _resolve_type

            type_cls = _resolve_type(source.source_type)
            if type_cls:
                return type_cls.thumbnail_url(source, art.file_id)
        except Exception as e:
            log.debug("_default_thumb_url dispatch falló, cayendo a gdrive: %s", e)

    return f"https://drive.google.com/thumbnail?id={art.file_id}&sz=w400"
