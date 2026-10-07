from __future__ import annotations

from fastapi import APIRouter

from mpc_forge.routes.decks import activity, art, crud, imports, localize, search, tokens

router = APIRouter()

router.include_router(search.router)
router.include_router(activity.router)
router.include_router(localize.router)
router.include_router(imports.router)
router.include_router(tokens.router)
router.include_router(art.router)
router.include_router(crud.router)

__all__ = ["router"]
