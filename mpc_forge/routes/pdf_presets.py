from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel

from mpc_forge.errors import ApiError, from_exception
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.printing import pdf_presets

router = APIRouter(prefix="/api/pdf-presets", tags=["pdf"])


class PdfPreset(BaseModel):
    name: str
    opts: dict[str, Any]


class SavePresetRequest(BaseModel):
    opts: dict[str, Any]


@router.get("/", response_model=list[PdfPreset])
async def list_pdf_presets(db: DbDep) -> list[PdfPreset]:
    return [PdfPreset.model_validate(p) for p in await pdf_presets.list_presets(db)]


@router.put("/{name}", response_model=list[PdfPreset])
async def save_pdf_preset(name: str, payload: SavePresetRequest, db: DbDep) -> list[PdfPreset]:
    try:
        presets = await pdf_presets.save_preset(db, name, payload.opts)
    except pdf_presets.PresetError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "invalid_preset") from e
    return [PdfPreset.model_validate(p) for p in presets]


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pdf_preset(name: str, db: DbDep) -> None:
    if not await pdf_presets.delete_preset(db, name):
        raise ApiError(status.HTTP_404_NOT_FOUND, "preset_not_found")
