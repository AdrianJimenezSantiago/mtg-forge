from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import FileResponse, RedirectResponse

from mpc_forge.config import PATHS
from mpc_forge.services import storage as storage_service
from mpc_forge.services import thumbnails

router = APIRouter(tags=["thumbnails"])
log = logging.getLogger(__name__)

_CACHE_CONTROL = "public, max-age=2592000, immutable"


def _resolve_within(base: Path, relative: str) -> Path:
    base_resolved = base.resolve()
    candidate = (base_resolved / relative).resolve()
    if not candidate.is_relative_to(base_resolved):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta no permitida")
    return candidate


@router.get("/api/thumb/{art_path:path}")
async def get_thumbnail(art_path: str) -> Response:
    if not art_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta vacía")

    source = _resolve_within(PATHS.art_dir, art_path)
    if not source.exists():
        source = _resolve_within(PATHS.custom_art_dir, art_path)
        if not source.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Arte no encontrado")

    thumb = await thumbnails.ensure_thumb(source)
    if thumb is None:
        original_url = (
            f"/art/{art_path}"
            if str(source).startswith(str(PATHS.art_dir))
            else f"/custom_art/{art_path}"
        )
        return RedirectResponse(original_url, status_code=status.HTTP_302_FOUND)

    return FileResponse(
        thumb,
        media_type="image/webp",
        headers={"Cache-Control": _CACHE_CONTROL},
    )


@router.get("/api/thumbs/stats")
async def thumbnail_stats() -> dict[str, int | bool | str]:
    data = thumbnails.stats()
    mb = round(int(data["bytes"]) / (1024 * 1024), 1)
    return {**data, "megabytes": str(mb)}


@router.post("/api/thumbs/clear")
async def clear_thumbnails() -> dict[str, int]:
    removed = thumbnails.clear()
    storage_service.invalidate()
    log.info("Caché de miniaturas vaciada: %d ficheros", removed)
    return {"removed": removed}
