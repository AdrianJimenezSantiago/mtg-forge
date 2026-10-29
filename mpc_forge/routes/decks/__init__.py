from __future__ import annotations

from fastapi import APIRouter

from mpc_forge.routes.decks import (
    activity,
    art,
    crud,
    custom_art_batch,
    imports,
    localize,
    search,
    tokens,
    tools,
)

router = APIRouter()

router.include_router(search.router)
router.include_router(activity.router)
router.include_router(localize.router)
router.include_router(imports.router)
router.include_router(tokens.router)
router.include_router(art.router)
router.include_router(custom_art_batch.router)
router.include_router(tools.router)
router.include_router(crud.router)

__all__ = ["router"]
