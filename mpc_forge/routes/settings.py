from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from mpc_forge import config as cfg
from mpc_forge.paths import install_root
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.system import settings as settings_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsResponse(BaseModel):
    definitions: list[dict[str, Any]]
    values: dict[str, Any]
    secrets_set: list[str] = []


class UpdateSettingsRequest(BaseModel):
    values: dict[str, Any]


class PathsResponse(BaseModel):
    install_root: str
    data_dir: str
    db_path: str
    art_dir: str
    custom_art_dir: str
    exports_dir: str
    backups_dir: str
    cardbacks_dir: str


@router.get("/", response_model=SettingsResponse)
async def get_settings(db: DbDep) -> SettingsResponse:
    values, secrets_set = settings_service.redact_values(await settings_service.get_all(db))
    return SettingsResponse(
        definitions=settings_service.definitions_dump(),
        values=values,
        secrets_set=secrets_set,
    )


@router.get("/paths", response_model=PathsResponse)
async def get_paths() -> PathsResponse:
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
        updated = await settings_service.set_many(db, payload.values)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    values, secrets_set = settings_service.redact_values(updated)
    return SettingsResponse(
        definitions=settings_service.definitions_dump(),
        values=values,
        secrets_set=secrets_set,
    )
