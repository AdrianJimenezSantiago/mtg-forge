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
import logging
from pathlib import Path

from mpc_forge.config import PATHS

log = logging.getLogger(__name__)

# Ancho al que se escalan las miniaturas. 160 px cubre con holgura la tarjeta
# de la rejilla (~120 px de ancho renderizado) incluso en pantallas 2x.
THUMB_WIDTH = 160
# Alto máximo derivado de la proporción de una carta de Magic (63×88 mm).
THUMB_HEIGHT = int(THUMB_WIDTH * 88 / 63)

# Calidad WebP. 72 es el punto donde el artefacto deja de ser visible a este
# tamaño; subir a 85 duplica el peso sin diferencia perceptible.
WEBP_QUALITY = 72
# method=4 equilibra tiempo de compresión y tamaño. El 6 (máximo) tarda el
# triple para ahorrar un 3%.
WEBP_METHOD = 4

_SUPPORTED_SOURCES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Límite de píxeles descomprimidos que aceptamos decodificar.
#
# Una "decompression bomb" es un PNG de pocos KB que declara 50000×50000 px:
# al decodificarlo, Pillow intenta reservar decenas de GB y el proceso muere.
# No es un ataque teórico aquí: `custom_art/` es una carpeta donde el usuario
# suelta ficheros que a menudo ha descargado de sitios de terceros.
#
# 80 Mpx deja sitio de sobra para cualquier escaneo legítimo de una carta
# (un 1200 dpi de una carta entera ronda los 12 Mpx) y corta el abuso.
MAX_SOURCE_PIXELS = 80_000_000

# Generaciones simultáneas. Cada una ocupa un hilo del executor por defecto de
# asyncio (`min(32, cpu+4)`), así que sin tope una rejilla con 300 artes sin
# cachear lo agota entero: el resto de `to_thread` de la app (exports a PDF,
# escaneo de custom art) se queda esperando detrás.
_GENERATION_CONCURRENCY = 4
_semaphore: asyncio.Semaphore | None = None

# Generaciones en vuelo, indexadas por ruta de destino. Sin esto, 40 tarjetas
# de la rejilla que comparten arte disparan 40 generaciones idénticas del
# mismo fichero, compitiendo por el mismo `.tmp`.
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


def thumb_path_for(source: Path) -> Path:
    """Ruta en disco donde vive (o vivirá) la miniatura de un arte.

    Se conserva la estructura de subdirectorios de ``art_dir`` para no meter
    decenas de miles de ficheros en una sola carpeta — algunos sistemas de
    ficheros se degradan mucho con directorios así de grandes.
    """
    try:
        relative = source.relative_to(PATHS.art_dir)
    except ValueError:
        # El arte está fuera de art_dir (arte custom, carpeta local del
        # usuario). Se agrupa por la inicial del nombre para repartir.
        relative = Path("_external") / source.name[:2].lower() / source.name
    return (PATHS.thumbs_dir / relative).with_suffix(".webp")


def _generate_sync(source: Path, target: Path) -> bool:
    """Genera la miniatura. Bloqueante: llamar siempre vía ``to_thread``."""
    from PIL import Image, ImageOps

    # Pillow avisa por encima de MAX_IMAGE_PIXELS y aborta al doble de ese
    # valor. Lo fijamos explícitamente en lugar de confiar en el default, que
    # depende de la versión instalada.
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS

    target.parent.mkdir(parents=True, exist_ok=True)
    # Fichero temporal + rename atómico: si el proceso muere a mitad, no queda
    # un WebP truncado que luego se sirva corrupto para siempre.
    tmp = target.with_suffix(".webp.tmp")
    with Image.open(source) as im:
        # exif_transpose respeta la orientación EXIF; algunos escaneos de arte
        # custom vienen rotados y sin esto la miniatura no coincide con la
        # imagen grande.
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
        # Si el arte original se ha regenerado (arte custom sustituido por el
        # usuario), la miniatura vieja quedaría obsoleta. Comparar mtime es
        # una comprobación barata que lo resuelve.
        try:
            if target.stat().st_mtime >= source.stat().st_mtime:
                return target
        except OSError:
            return target

    if not pillow_available():
        return None

    # Si ya hay una generación en curso para este mismo destino, esperamos a
    # esa en vez de lanzar otra. Es el mismo patrón de deduplicación en vuelo
    # que usa `ScryfallClient._inflight`.
    existing = _inflight.get(target)
    if existing is not None:
        try:
            return await asyncio.shield(existing)
        except Exception:
            return None

    async def _generate() -> Path | None:
        async with _get_semaphore():
            # Puede haberla generado otro esperando en el semáforo.
            if target.exists():
                return target
            try:
                await asyncio.to_thread(_generate_sync, source, target)
                return target
            except Exception:
                # Una imagen corrupta, una bomba de descompresión o un formato
                # exótico no deben romper la carga de la rejilla entera. Se
                # registra y se cae a la imagen original.
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


async def warm_many(sources: list[Path], *, concurrency: int = 4) -> int:
    """Pregenera miniaturas en lote. Devuelve cuántas se crearon.

    Se usa después de descargar el arte de un mazo: para cuando el usuario abra
    el selector, las miniaturas ya están listas. La concurrencia es baja a
    propósito — es trabajo de CPU y no queremos competir con las descargas.
    """
    if not pillow_available():
        return 0
    semaphore = asyncio.Semaphore(concurrency)
    created = 0

    async def _one(path: Path) -> None:
        nonlocal created
        async with semaphore:
            if not thumb_path_for(path).exists():
                if await ensure_thumb(path) is not None:
                    created += 1

    await asyncio.gather(*(_one(p) for p in sources), return_exceptions=True)
    return created


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
