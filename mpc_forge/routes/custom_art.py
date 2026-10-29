from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from mpc_forge.models import CustomArt
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.schemas import AddCustomArtFromUrlRequest, RescanResult
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.art import custom_art_folders, thumbnails
from mpc_forge.services.art.name_matching import card_keys

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
        stmt = stmt.where(CustomArt.card_name_normalized.in_(card_keys(card_name).all))
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
    if custom_art_service.is_linked(ca):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "linked_file",
                "message": "Este arte está en una carpeta vinculada: bórralo de esa carpeta.",
            },
        )
    custom_art_service.remove_file(ca)
    await custom_art_service.delete_arts(db, [ca.id])
    await db.commit()


class ZipImportSkipped(BaseModel):
    name: str
    code: str


class ZipImportResultView(BaseModel):
    art_ids: list[int]
    added: int
    duplicates: int
    skipped: list[ZipImportSkipped]


@router.post("/import-zip", response_model=ZipImportResultView)
async def import_zip(db: DbDep, file: Annotated[UploadFile, File()]) -> ZipImportResultView:
    try:
        result = await custom_art_service.import_zip(db, file.file, file.filename or "zip")
    except custom_art_service.UploadRejected as e:
        raise HTTPException(e.status_code, {"code": e.code, "message": str(e)}) from e
    finally:
        await file.close()
    return ZipImportResultView.model_validate(result.to_dict())


class LinkedFolderView(BaseModel):
    id: int
    path: str
    label: str
    file_count: int
    online: bool
    in_use: int
    last_scanned_at: str | None


class LinkFolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    label: str | None = Field(default=None, max_length=256)


class UnlinkFolderResult(BaseModel):
    removed_images: int


@router.get("/folders", response_model=list[LinkedFolderView])
async def list_linked_folders(db: DbDep) -> list[LinkedFolderView]:
    return [LinkedFolderView(**f.to_dict()) for f in await custom_art_folders.list_folders(db)]


@router.post("/folders", response_model=LinkedFolderView, status_code=status.HTTP_201_CREATED)
async def link_folder(payload: LinkFolderRequest, db: DbDep) -> LinkedFolderView:
    try:
        folder = await custom_art_folders.add_folder(db, payload.path, payload.label)
    except custom_art_folders.FolderRejected as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": e.code, "message": str(e)}) from e
    return LinkedFolderView(**folder.to_dict())


@router.delete("/folders/{folder_id}", response_model=UnlinkFolderResult)
async def unlink_folder(folder_id: int, db: DbDep) -> UnlinkFolderResult:
    removed = await custom_art_folders.remove_folder(db, folder_id)
    if removed is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carpeta no encontrada")
    return UnlinkFolderResult(removed_images=removed)
