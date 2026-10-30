from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.system import whats_new

router = APIRouter(prefix="/api/whats-new", tags=["system"])


class WhatsNew(BaseModel):
    version: str
    previous: str
    notes: str | None = None
    url: str


@router.get("/", response_model=WhatsNew | None)
async def get_whats_new(db: DbDep) -> WhatsNew | None:
    data = await whats_new.pending(db)
    return WhatsNew(**data) if data else None


@router.post("/seen", status_code=status.HTTP_204_NO_CONTENT)
async def whats_new_seen(db: DbDep) -> None:
    await whats_new.mark_seen(db)
