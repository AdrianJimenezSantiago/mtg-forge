"""Miniaturas WebP locales para las rejillas de la interfaz.

El problema
-----------
El selector de arte de una carta muy reimpresa puede tener 900 opciones. Cada
tarjeta de la rejilla cargaba la imagen ``small`` desde el CDN de Scryfall
(~90 KB) o desde el thumbnail de Google Drive. Eso significaba, para una
rejilla de 200 tarjetas visibles:

* ~18 MB de tráfico
* 200 conexiones a servidores de terceros
* dependencia total de la red para algo que ya tenemos en disco

La solución
-----------
Generar una miniatura WebP de 160 px de ancho a partir del arte que YA está
descargado en ``art_dir``. Una miniatura pesa unos 6 KB, se sirve desde
``localhost`` y funciona sin conexión. La misma rejilla baja a ~1,2 MB.

Diseño
------
* **Perezoso.** No se generan miniaturas al descargar el arte (encarecería
  importar un mazo). Se generan la primera vez que alguien las pide, y a partir
  de ahí quedan en disco.
* **Derivadas y desechables.** ``thumbs_dir`` se puede borrar entero en
  cualquier momento: se regenera solo. Por eso no entra en los backups.
* **Degradación limpia.** Si Pillow no está instalado, ``thumb_url_for``
  devuelve ``None`` y la interfaz cae a la imagen original. La app funciona
  igual, solo que consume más ancho de banda.
* **Nombre determinista.** La miniatura se llama igual que el arte de origen
  con extensión ``.webp``, así que no hace falta consultar la BD para saber si
  existe: basta con mirar el disco.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from mpc_forge.config import PATHS

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
    """Semáforo perezoso, creado dentro del event loop que lo va a usar.

    Instanciarlo a nivel de módulo lo ataría al loop que estuviera activo en el
    import, que no tiene por qué ser el de la app (en los tests hay uno por
    test).
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_GENERATION_CONCURRENCY)
    return _semaphore


def pillow_available() -> bool:
    """¿Está Pillow instalado?

    Es una dependencia opcional (compartida con la feature de pHash). Sin ella
    la app funciona, simplemente sin miniaturas.
    """
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def _relative_to_art_dir(source: Path) -> Path | None:
    """``source`` relativo a ``art_dir``, o None si está fuera.

    Se intenta primero con las rutas tal cual (el caso normal, sin coste) y,
    si falla, con ambas resueltas.

    El segundo intento no es paranoia: ``relative_to`` compara cadenas, y en
    Windows la MISMA carpeta se puede escribir de dos formas. El endpoint de
    miniaturas resuelve la ruta pedida para impedir el escape de directorio, y
    ``resolve()`` expande los nombres cortos 8.3
    (la forma ``RUNNER~1`` pasa a ser ``runneradmin``). Si
    ``art_dir`` estaba guardado en la forma corta, la comparación fallaba y
    TODAS las miniaturas acababan en el cajón ``_external`` — perdiendo el
    reparto por subdirectorios y, peor, colisionando entre sí (dos
    "Sol Ring.png" de carpetas distintas compartían miniatura, así que la
    rejilla mostraba el arte equivocado).
    """
    try:
        return source.relative_to(PATHS.art_dir)
    except ValueError:
        pass
    try:
        return source.resolve().relative_to(PATHS.art_dir.resolve())
    except (ValueError, OSError):
        return None


def thumb_path_for(source: Path) -> Path:
    """Ruta en disco donde vive (o vivirá) la miniatura de un arte.

    Se conserva la estructura de subdirectorios de ``art_dir`` para no meter
    decenas de miles de ficheros en una sola carpeta — algunos sistemas de
    ficheros se degradan mucho con directorios así de grandes.
    """
    relative = _relative_to_art_dir(source)
    if relative is None:
        digest = hashlib.sha256(
            str(source.parent).encode("utf-8", "surrogateescape")
        ).hexdigest()[:8]
        stem = Path(source.name).stem
        relative = (
            Path("_external") / source.name[:2].lower() / f"{stem}-{digest}.webp"
        )
    return (PATHS.thumbs_dir / relative).with_suffix(".webp")


class SourceTooLargeError(Exception):
    """La imagen declara más píxeles de los que estamos dispuestos a decodificar."""


def _generate_sync(source: Path, target: Path) -> bool:
    """Genera la miniatura. Bloqueante: llamar siempre vía ``to_thread``."""
    from PIL import Image, ImageOps

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".webp.tmp")
    with Image.open(source) as im:
        width, height = im.size
        if width * height > MAX_SOURCE_PIXELS:
            raise SourceTooLargeError(
                f"{source.name}: {width}x{height} px supera el límite de "
                f"{MAX_SOURCE_PIXELS} px"
            )
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        im.thumbnail((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS)
        im.save(tmp, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    tmp.replace(target)
    return True


async def ensure_thumb(source: Path) -> Path | None:
    """Devuelve la miniatura de ``source``, generándola si aún no existe.

    Devuelve ``None`` si no se puede generar (sin Pillow, formato no
    soportado, fichero ilegible). El caller debe caer a la imagen original.
    """
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
                log.warning(
                    "No se pudo generar la miniatura de %s", source.name, exc_info=True
                )
                return None

    task = asyncio.ensure_future(_generate())
    _inflight[target] = task
    try:
        return await task
    finally:
        _inflight.pop(target, None)


def thumb_url(source: Path) -> str | None:
    """URL pública de la miniatura, si ya está en disco.

    Versión no bloqueante para usar en serializadores: no genera nada, solo
    comprueba la existencia. La generación la dispara el endpoint
    ``/thumb/...``, que sí puede esperar.
    """
    target = thumb_path_for(source)
    if not target.exists():
        return None
    relative = target.relative_to(PATHS.thumbs_dir).as_posix()
    return f"/thumbs/{relative}"


def url_for_relative(art_relative_path: str) -> str:
    """URL de la miniatura de un ``LocalArt``, se haya generado o no.

    Apunta siempre al endpoint ``/api/thumb/`` que genera bajo demanda. Es lo
    que consumen las rejillas: piden la miniatura y el backend decide si la
    sirve del disco o la crea en ese momento.

    Los ``relative_path`` guardados en Windows llevan separador ``\\``; una URL
    siempre usa ``/``, así que se normaliza aquí en vez de en cada llamada.
    """
    normalized = art_relative_path.replace("\\", "/").lstrip("/")
    return f"/api/thumb/{normalized}"


def stats() -> dict[str, int | bool]:
    """Métricas para la vista de Ajustes."""
    if not PATHS.thumbs_dir.exists():
        return {"count": 0, "bytes": 0, "available": pillow_available()}
    count = 0
    total = 0
    for f in PATHS.thumbs_dir.rglob("*.webp"):
        count += 1
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return {"count": count, "bytes": total, "available": pillow_available()}


def clear() -> int:
    """Borra todas las miniaturas. Devuelve cuántas se eliminaron.

    Es seguro: son datos derivados que se regeneran al vuelo.
    """
    if not PATHS.thumbs_dir.exists():
        return 0
    removed = 0
    for f in PATHS.thumbs_dir.rglob("*.webp"):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed
