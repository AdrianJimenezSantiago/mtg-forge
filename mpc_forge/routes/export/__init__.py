from __future__ import annotations

from fastapi import APIRouter

from mpc_forge.routes.export import cardbacks, decklist, files, pdf, press, xml

router = APIRouter()

for module in (xml, decklist, files, cardbacks, pdf, press):
    router.include_router(module.router)

__all__ = ["router"]
