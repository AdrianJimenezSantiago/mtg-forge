"""Endpoints REST para gestionar el arte custom local."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.db import get_session
from mpc_forge.models import CustomArt
from mpc_forge.schemas import AddCustomArtFromUrlRequest, RescanResult
from mpc_forge.services import custom_art as custom_art_service

router = APIRouter(prefix="/api/custom-art", tags=["custom-art"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/rescan", response_model=RescanResult)
async def rescan(db: DbDep) -> RescanResult:
    """Reindexa la carpeta de arte custom (%APPDATA%\\MPC-Forge\\custom_art\\)."""
    stats = await custom_art_service.rescan(db)
    return RescanResult(**stats)


@router.post("/from-url", response_model=dict)
async def add_from_url(payload: AddCustomArtFromUrlRequest, db: DbDep) -> dict:
    """Descarga una imagen de una URL y la guarda como arte custom."""
    try:
        art = await custom_art_service.add_from_url(
            db,
            url=payload.url,
            card_name=payload.card_name,
            face=payload.face,
            variant=payload.variant,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Error descargando: {e}")
    return {
        "id": art.id,
        "filename": art.filename,
        "relative_path": art.relative_path,
        "card_name_normalized": art.card_name_normalized,
        "variant_label": art.variant_label,
        "face": art.face,
        "image_url": f"/custom_art/{art.relative_path}",
    }


class CustomArtListItem(BaseModel):
    id: int
    filename: str
    relative_path: str
    card_name_normalized: str
    variant_label: str | None
    face: str
    bytes_size: int
    image_url: str


@router.get("/", response_model=list[CustomArtListItem])
async def list_custom_arts(db: DbDep, card_name: str | None = None) -> list[CustomArtListItem]:
    """Lista todos los custom arts, opcionalmente filtrados por nombre de carta."""
    stmt = select(CustomArt).order_by(CustomArt.card_name_normalized, CustomArt.filename)
    if card_name:
        stmt = stmt.where(
            CustomArt.card_name_normalized == custom_art_service.normalize_card_name(card_name)
        )
    rows = (await db.scalars(stmt)).all()
    return [
        CustomArtListItem(
            id=r.id,
            filename=r.filename,
            relative_path=r.relative_path,
            card_name_normalized=r.card_name_normalized,
            variant_label=r.variant_label,
            face=r.face,
            bytes_size=r.bytes_size,
            image_url=f"/custom_art/{r.relative_path}",
        )
        for r in rows
    ]


@router.delete("/{custom_art_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_art(custom_art_id: int, db: DbDep) -> None:
    """Elimina un custom art (del índice y del disco)."""
    ca = await db.get(CustomArt, custom_art_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    abs_p = custom_art_service.absolute_path(ca)
    if abs_p.exists():
        try:
            abs_p.unlink()
        except OSError:
            pass
    await db.delete(ca)
    await db.commit()
