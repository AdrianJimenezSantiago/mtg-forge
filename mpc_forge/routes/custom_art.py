from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select

from mpc_forge.models import CustomArt
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.schemas import AddCustomArtFromUrlRequest, RescanResult
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.art import thumbnails

router = APIRouter(prefix="/api/custom-art", tags=["custom-art"])


@router.post("/rescan", response_model=RescanResult)
async def rescan(db: DbDep) -> RescanResult:
    stats = await custom_art_service.rescan(db)
    return RescanResult(**stats)


@router.post("/from-url", response_model=dict)
async def add_from_url(payload: AddCustomArtFromUrlRequest, db: DbDep) -> dict[str, Any]:
    try:
        art = await custom_art_service.add_from_url(
            db,
            url=payload.url,
            card_name=payload.card_name,
            face=payload.face,
            variant=payload.variant,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Error descargando: {e}") from e
    return {
        "id": art.id,
        "filename": art.filename,
        "relative_path": art.relative_path,
        "card_name_normalized": art.card_name_normalized,
        "variant_label": art.variant_label,
        "face": art.face,
        "image_url": custom_art_service.custom_art_url(art.relative_path),
    }


class UploadWarning(BaseModel):
    code: str
    ratio: float | None = None
    expected: float | None = None
    dpi: int | None = None
    recommended: int | None = None


class UploadedCustomArt(BaseModel):
    id: int
    filename: str
    relative_path: str
    card_name_normalized: str
    variant_label: str | None
    face: str
    bytes_size: int
    image_url: str
    thumb_url: str
    width: int
    height: int
    dpi: int
    adjusted: bool
    duplicate: bool
    warnings: list[UploadWarning]


@router.post("/upload", response_model=UploadedCustomArt)
async def upload_custom_art(
    db: DbDep,
    file: Annotated[UploadFile, File()],
    card_name: Annotated[str, Form(min_length=1, max_length=200)],
    face: Annotated[Literal["front", "back"], Form()] = "front",
    variant: Annotated[str | None, Form(max_length=120)] = None,
    fit: Annotated[Literal["stretch", "crop", "contain"], Form()] = "stretch",
) -> UploadedCustomArt:
    data = await file.read(custom_art_service.MAX_UPLOAD_BYTES + 1)
    await file.close()
    try:
        art, prepared, duplicate = await custom_art_service.add_from_upload(
            db, data, card_name=card_name, face=face, variant=variant, fit=fit
        )
    except custom_art_service.UploadRejected as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": str(e)}) from e
    return UploadedCustomArt(
        id=art.id,
        filename=art.filename,
        relative_path=art.relative_path,
        card_name_normalized=art.card_name_normalized,
        variant_label=art.variant_label,
        face=art.face,
        bytes_size=art.bytes_size,
        image_url=custom_art_service.custom_art_url(art.relative_path),
        thumb_url=thumbnails.url_for_relative(art.relative_path),
        width=prepared.width,
        height=prepared.height,
        dpi=prepared.dpi,
        adjusted=prepared.adjusted,
        duplicate=duplicate,
        warnings=[UploadWarning(**w) for w in prepared.warnings],
    )


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
            image_url=custom_art_service.custom_art_url(r.relative_path),
        )
        for r in rows
    ]


@router.delete("/{custom_art_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_art(custom_art_id: int, db: DbDep) -> None:
    ca = await db.get(CustomArt, custom_art_id)
    if not ca:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    custom_art_service.remove_file(ca)
    await db.delete(ca)
    await db.commit()
