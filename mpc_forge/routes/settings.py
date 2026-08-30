"""Endpoints REST para leer y editar los ajustes runtime."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.db import get_session
from mpc_forge.paths import install_root
from mpc_forge.services import settings as settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


class SettingsResponse(BaseModel):
    definitions: list[dict[str, Any]]
    values: dict[str, Any]


class UpdateSettingsRequest(BaseModel):
    values: dict[str, Any]


class PathsResponse(BaseModel):
    """Rutas efectivas que la app está usando ahora mismo (defaults + overrides).

    Sirve para pintar en la UI las rutas reales — antes estaban hardcodeadas
    como ``%APPDATA%\\MPC-Forge\\...`` en el template, lo que se rompía en cuanto
    el usuario personalizaba una. Todos los valores son strings con la ruta
    absoluta resuelta.
    """
    install_root: str        # Carpeta del .exe o proyecto
    data_dir: str            # Raíz de datos (contiene BD y logs) — no editable
    db_path: str
    art_dir: str
    custom_art_dir: str
    exports_dir: str
    backups_dir: str
    cardbacks_dir: str


@router.get("/", response_model=SettingsResponse)
async def get_settings(db: DbDep) -> SettingsResponse:
    return SettingsResponse(
        definitions=settings_service.definitions_dump(),
        values=await settings_service.get_all(db),
    )


@router.get("/paths", response_model=PathsResponse)
async def get_paths() -> PathsResponse:
    """Snapshot de las rutas efectivas.

    Se lee directamente de ``cfg.PATHS`` sin tocar BD — cfg.PATHS refleja ya
    los overrides que el usuario haya puesto (apply_to_config los aplica al
    guardar y al arrancar la app).
    """
    return PathsResponse(
        install_root=str(install_root()),
        data_dir=str(cfg.PATHS.data_dir),
        db_path=str(cfg.PATHS.db_path),
        art_dir=str(cfg.PATHS.art_dir),
        custom_art_dir=str(cfg.PATHS.custom_art_dir),
        exports_dir=str(cfg.PATHS.exports_dir),
        backups_dir=str(cfg.PATHS.backups_dir),
        cardbacks_dir=str(cfg.PATHS.cardbacks_dir),
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
