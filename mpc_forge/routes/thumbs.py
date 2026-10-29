from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Response, status
from fastapi.responses import FileResponse, RedirectResponse

from mpc_forge import config as cfg
from mpc_forge.errors import ApiError
from mpc_forge.services.art import custom_art, thumbnails
from mpc_forge.services.system import storage as storage_service

router = APIRouter(tags=["thumbnails"])
log = logging.getLogger(__name__)

_CACHE_CONTROL = "public, max-age=2592000, immutable"


def _resolve_within(base: Path, relative: str) -> Path:
    base_resolved = base.resolve()
    candidate = (base_resolved / relative).resolve()
    if not candidate.is_relative_to(base_resolved):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "path_not_allowed")
    return candidate


@router.get("/api/thumb/{art_path:path}")
async def get_thumbnail(art_path: str) -> Response:
    if not art_path:
        raise ApiError(status.HTTP_404_NOT_FOUND, "empty_path")

    source = _resolve_within(cfg.PATHS.art_dir, art_path)
    if not source.exists():
        custom = custom_art.resolve_relative(art_path)
        if custom is None or not custom.exists():
            raise ApiError(status.HTTP_404_NOT_FOUND, "art_not_found")
        source = custom

    thumb = await thumbnails.ensure_thumb(source)
    if thumb is None:
        original_url = (
            f"/art/{art_path}"
            if str(source).startswith(str(cfg.PATHS.art_dir))
            else f"/custom_art/{art_path}"
        )
        return RedirectResponse(original_url, status_code=status.HTTP_302_FOUND)

    linked = custom_art.linked_folder_id(art_path) is not None
    return FileResponse(
        thumb,
        media_type="image/webp",
        headers={"Cache-Control": "no-cache" if linked else _CACHE_CONTROL},
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
