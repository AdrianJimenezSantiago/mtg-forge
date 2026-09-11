"""Endpoint de miniaturas: sirve WebP de 160 px generados bajo demanda.

Se separa de los ``StaticFiles`` montados en ``app.py`` porque las miniaturas
no existen hasta que alguien las pide por primera vez: hace falta lógica, no un
servidor de ficheros estático.

Contrato:
    GET /api/thumb/<ruta relativa dentro de art_dir>

Si la miniatura existe se sirve del disco. Si no, se genera en ese momento y se
sirve. Si no se puede generar (sin Pillow, imagen corrupta), se redirige a la
imagen original para que la rejilla nunca muestre un hueco.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse, RedirectResponse

from mpc_forge.config import PATHS
from mpc_forge.services import thumbnails

router = APIRouter(tags=["thumbnails"])
log = logging.getLogger(__name__)

# Las miniaturas son inmutables en la práctica: el nombre deriva del hash del
# arte de origen. `immutable` hace que el navegador ni siquiera lance la
# petición condicional al refrescar — con 300 tarjetas en pantalla eso son 300
# peticiones 304 que desaparecen.
_CACHE_CONTROL = "public, max-age=2592000, immutable"


def _resolve_within(base: Path, relative: str) -> Path:
    """Resuelve ``relative`` dentro de ``base`` rechazando el escape.

    Sin esta comprobación, una petición a ``/api/thumb/../../../etc/passwd``
    dejaría leer cualquier fichero del sistema. Se resuelven ambas rutas a
    absoluto y se verifica la relación de ancestro: comprobar solo la presencia
    de ``..`` en la cadena no basta, porque los enlaces simbólicos y la
    codificación de la URL pueden esquivarlo.
    """
    base_resolved = base.resolve()
    candidate = (base_resolved / relative).resolve()
    if not candidate.is_relative_to(base_resolved):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta no permitida")
    return candidate


@router.get("/api/thumb/{art_path:path}")
async def get_thumbnail(art_path: str) -> Response:
    """Devuelve la miniatura de un arte, generándola si es la primera vez."""
    if not art_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta vacía")

    source = _resolve_within(PATHS.art_dir, art_path)
    if not source.exists():
        # Puede ser arte custom en lugar de arte de Scryfall: se intenta en el
        # otro directorio antes de rendirse.
        source = _resolve_within(PATHS.custom_art_dir, art_path)
        if not source.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Arte no encontrado")

    thumb = await thumbnails.ensure_thumb(source)
    if thumb is None:
        # Degradación limpia: sin Pillow o con una imagen que no se puede
        # procesar, se manda al cliente a la imagen original. Pesa más, pero la
        # interfaz se ve correcta.
        original_url = f"/art/{art_path}" if str(source).startswith(
            str(PATHS.art_dir)
        ) else f"/custom_art/{art_path}"
        return RedirectResponse(original_url, status_code=status.HTTP_302_FOUND)

    return FileResponse(
        thumb,
        media_type="image/webp",
        headers={"Cache-Control": _CACHE_CONTROL},
    )


@router.get("/api/thumbs/stats")
async def thumbnail_stats() -> dict[str, int | bool | str]:
    """Cuántas miniaturas hay y cuánto ocupan. Se muestra en Ajustes."""
    data = thumbnails.stats()
    mb = round(int(data["bytes"]) / (1024 * 1024), 1)
    return {**data, "megabytes": str(mb)}


@router.post("/api/thumbs/clear")
async def clear_thumbnails() -> dict[str, int]:
    """Vacía la caché de miniaturas.

    Es una operación segura y sin confirmación destructiva real: las
    miniaturas son datos derivados y se regeneran solas la próxima vez que se
    abra una rejilla.
    """
    removed = thumbnails.clear()
    log.info("Caché de miniaturas vaciada: %d ficheros", removed)
    return {"removed": removed}
