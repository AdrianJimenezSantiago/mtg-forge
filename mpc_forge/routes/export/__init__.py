from __future__ import annotations

from fastapi import APIRouter

from mpc_forge.routes.export import cardbacks, decklist, files, pdf, xml

router = APIRouter()

for module in (xml, decklist, files, cardbacks, pdf):
    router.include_router(module.router)

__all__ = ["router"]
