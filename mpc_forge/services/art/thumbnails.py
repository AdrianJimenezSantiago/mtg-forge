from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from mpc_forge import config as cfg

log = logging.getLogger(__name__)

THUMB_WIDTH = 160
THUMB_HEIGHT = int(THUMB_WIDTH * 88 / 63)

WEBP_QUALITY = 72
WEBP_METHOD = 4

_SUPPORTED_SOURCES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

MAX_SOURCE_PIXELS = 80_000_000

_GENERATION_CONCURRENCY = 4
_semaphore: asyncio.Semaphore | None = None

_inflight: dict[Path, asyncio.Task] = {}


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_GENERATION_CONCURRENCY)
    return _semaphore


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


def _relative_to_art_dir(source: Path) -> Path | None:
    try:
        return source.relative_to(cfg.PATHS.art_dir)
    except ValueError:
        pass
    try:
        return source.resolve().relative_to(cfg.PATHS.art_dir.resolve())
    except (ValueError, OSError):
        return None


def thumb_path_for(source: Path) -> Path:
    relative = _relative_to_art_dir(source)
    if relative is None:
        digest = hashlib.sha256(str(source.parent).encode("utf-8", "surrogateescape")).hexdigest()[
            :8
        ]
        stem = Path(source.name).stem
        relative = Path("_external") / source.name[:2].lower() / f"{stem}-{digest}.webp"
    return (cfg.PATHS.thumbs_dir / relative).with_suffix(".webp")


class SourceTooLargeError(Exception):
    pass


def _generate_sync(source: Path, target: Path) -> bool:
    from PIL import Image, ImageOps

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".webp.tmp")
    with Image.open(source) as im:
        width, height = im.size
        if width * height > MAX_SOURCE_PIXELS:
            raise SourceTooLargeError(
                f"{source.name}: {width}x{height} px supera el límite de {MAX_SOURCE_PIXELS} px"
            )
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        im.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
        im.save(tmp, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    tmp.replace(target)
    return True


async def ensure_thumb(source: Path) -> Path | None:
    if not source.exists() or source.suffix.lower() not in _SUPPORTED_SOURCES:
        return None

    target = thumb_path_for(source)
    if target.exists():
        try:
            if target.stat().st_mtime >= source.stat().st_mtime:
                return target
        except OSError:
            return target

    if not pillow_available():
        return None

    existing = _inflight.get(target)
    if existing is not None:
        try:
            return await asyncio.shield(existing)
        except Exception:
            return None

    async def _generate() -> Path | None:
        async with _get_semaphore():
            if target.exists():
                return target
            try:
                await asyncio.to_thread(_generate_sync, source, target)
                return target
            except Exception:
                log.warning("No se pudo generar la miniatura de %s", source.name, exc_info=True)
                return None

    task = asyncio.ensure_future(_generate())
    _inflight[target] = task
    try:
        return await task
    finally:
        _inflight.pop(target, None)


def thumb_url(source: Path) -> str | None:
    target = thumb_path_for(source)
    if not target.exists():
        return None
    relative = target.relative_to(cfg.PATHS.thumbs_dir).as_posix()
    return f"/thumbs/{relative}"


def url_for_relative(art_relative_path: str) -> str:
    normalized = art_relative_path.replace("\\", "/").lstrip("/")
    return f"/api/thumb/{normalized}"


def stats() -> dict[str, int | bool]:
    if not cfg.PATHS.thumbs_dir.exists():
        return {"count": 0, "bytes": 0, "available": pillow_available()}
    count = 0
    total = 0
    for f in cfg.PATHS.thumbs_dir.rglob("*.webp"):
        count += 1
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return {"count": count, "bytes": total, "available": pillow_available()}


def clear() -> int:
    if not cfg.PATHS.thumbs_dir.exists():
        return 0
    removed = 0
    for f in cfg.PATHS.thumbs_dir.rglob("*.webp"):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed
