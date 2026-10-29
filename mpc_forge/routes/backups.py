from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from mpc_forge import config as cfg
from mpc_forge import db as db_module
from mpc_forge.errors import ApiError, from_exception
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.system import backup as backup_service
from mpc_forge.services.system import restore as restore_service
from mpc_forge.services.system import settings as settings_service
from mpc_forge.services.system import storage as storage_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backups", tags=["backups"])

MAX_UPLOAD_BYTES = 4 * 1024**3


class BackupItem(BaseModel):
    filename: str
    bytes_size: int
    created_at: str
    automatic: bool
    tag: str


class BackupSummaryView(BaseModel):
    filename: str
    created_at: str
    bytes_size: int
    schema_version: int | None
    decks: int
    cards: int
    collection: int
    custom_arts: int
    snapshots: int
    art_files: int


class RestoreResponse(BaseModel):
    summary: BackupSummaryView
    safety_backup: str
    restored_art_files: int
    migrations_applied: list[Any] = []


def _resolve(filename: str) -> Path:
    try:
        return restore_service.resolve_backup(filename)
    except restore_service.RestoreError as e:
        raise from_exception(status.HTTP_404_NOT_FOUND, e, "backup_not_found") from e


async def _summary(path: Path) -> BackupSummaryView:
    try:
        summary = await asyncio.to_thread(restore_service.inspect, path)
    except restore_service.RestoreError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "backup_invalid") from e
    return BackupSummaryView.model_validate(summary.to_dict())


@router.get("/", response_model=list[BackupItem])
async def list_backups() -> list[BackupItem]:
    return [BackupItem(**b) for b in backup_service.list_backups()]


@router.get("/{filename}", response_model=BackupSummaryView)
async def inspect_backup(filename: str) -> BackupSummaryView:
    return await _summary(_resolve(filename))


@router.get("/{filename}/download")
async def download_backup(filename: str) -> FileResponse:
    path = _resolve(filename)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/{filename}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backup(filename: str) -> None:
    _resolve(filename).unlink()
    storage_service.invalidate()


@router.post("/upload", response_model=BackupSummaryView)
async def upload_backup(file: Annotated[UploadFile, File()]) -> BackupSummaryView:
    cfg.PATHS.backups_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=cfg.PATHS.backups_dir, suffix=".part", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
        written = 0
        try:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ApiError(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "backup_too_large")
                tmp.write(chunk)
        except BaseException:
            tmp.close()
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
    try:
        await asyncio.to_thread(restore_service.inspect, tmp_path)
    except restore_service.RestoreError as e:
        tmp_path.unlink(missing_ok=True)
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "backup_invalid") from e
    saved = await asyncio.to_thread(restore_service.save_upload, tmp_path)
    storage_service.invalidate()
    return await _summary(saved)


@router.post("/{filename}/restore", response_model=RestoreResponse)
async def restore_backup(filename: str) -> RestoreResponse:
    path = _resolve(filename)
    try:
        result = await asyncio.to_thread(restore_service.restore_files, path)
    except restore_service.RestoreError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "restore_failed") from e
    except OSError as e:
        log.exception("La restauración del backup %s falló", filename)
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "restore_failed", reason=str(e)
        ) from e

    await db_module.init_db()
    settings_service.invalidate_cache()
    async with db_module.session_scope() as db:
        settings_service.apply_to_config(await settings_service.get_all(db))
        await custom_art_service.rescan(db)
    storage_service.invalidate()
    applied = db_module.LAST_MIGRATION_REPORT.get("applied")
    return RestoreResponse(
        summary=BackupSummaryView.model_validate(result.summary.to_dict()),
        safety_backup=result.safety_backup,
        restored_art_files=result.restored_art_files,
        migrations_applied=list(applied) if isinstance(applied, list) else [],
    )
