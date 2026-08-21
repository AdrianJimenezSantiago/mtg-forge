"""Endpoints REST para leer y editar los ajustes runtime."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.db import get_session
from mpc_forge.services import settings as settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


class SettingsResponse(BaseModel):
    definitions: list[dict[str, Any]]
    values: dict[str, Any]


class UpdateSettingsRequest(BaseModel):
    values: dict[str, Any]


@router.get("/", response_model=SettingsResponse)
async def get_settings(db: DbDep) -> SettingsResponse:
    return SettingsResponse(
        definitions=settings_service.definitions_dump(),
        values=await settings_service.get_all(db),
    )


@router.put("/", response_model=SettingsResponse)
async def update_settings(payload: UpdateSettingsRequest, db: DbDep) -> SettingsResponse:
    try:
        values = await settings_service.set_many(db, payload.values)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return SettingsResponse(
        definitions=settings_service.definitions_dump(),
        values=values,
    )
