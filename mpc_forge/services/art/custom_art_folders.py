"""Carpetas del usuario vinculadas como arte custom, sin copiar sus ficheros.

Las imágenes se indexan con `relative_path = _linked/<id>/<ruta>` y se sirven
desde la carpeta original, así que lo que el usuario cambie allí se refleja al
re-escanear. La app nunca borra ni modifica esos ficheros.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import CustomArt, CustomArtFolder, DeckCard
from mpc_forge.services.art import custom_art
from mpc_forge.services.indexing.source_types.local_folder import LocalFolderSourceType

log = logging.getLogger(__name__)

MAX_LINKED_FOLDERS = 50


class FolderRejected(ValueError):
    """La carpeta no se puede vincular. `code` es estable para la interfaz."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class FolderView:
    id: int
    path: str
    label: str
    file_count: int
    online: bool
    in_use: int
    last_scanned_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _validate_path(raw: str) -> Path:
    try:
        path = Path(LocalFolderSourceType.validate_url(raw))
    except ValueError as e:
        raise FolderRejected("not_a_folder", str(e)) from e
    if path.parent == path:
        raise FolderRejected("too_broad", "No se puede vincular la raíz de un disco.")
    if path == Path.home().resolve():
        raise FolderRejected("too_broad", "Vincula una carpeta concreta, no tu carpeta personal.")
    app_root = cfg.PATHS.custom_art_dir.resolve()
    if _overlaps(path, app_root):
        raise FolderRejected(
            "inside_app_folder",
            "Esa carpeta ya forma parte del arte custom de la app; no hace falta vincularla.",
        )
    return path


async def _usage_by_folder(db: AsyncSession, folder_ids: list[int]) -> dict[int, int]:
    """Cuántas cartas de mazos usan arte de cada carpeta."""
    out: dict[int, int] = {}
    for folder_id in folder_ids:
        prefix = custom_art.linked_relative_path(folder_id, "")
        ids = select(CustomArt.id).where(
            CustomArt.relative_path.startswith(prefix, autoescape=True)
        )
        count = await db.scalar(
            select(func.count(DeckCard.id)).where(
                or_(DeckCard.custom_art_front_id.in_(ids), DeckCard.custom_art_back_id.in_(ids))
            )
        )
        out[folder_id] = int(count or 0)
    return out


async def list_folders(db: AsyncSession) -> list[FolderView]:
    folders = (await db.scalars(select(CustomArtFolder).order_by(CustomArtFolder.label))).all()
    usage = await _usage_by_folder(db, [f.id for f in folders])
    online = await asyncio.gather(*(asyncio.to_thread(Path(f.path).is_dir) for f in folders))
    return [
        FolderView(
            id=f.id,
            path=f.path,
            label=f.label,
            file_count=f.file_count,
            online=is_online,
            in_use=usage.get(f.id, 0),
            last_scanned_at=f.last_scanned_at.isoformat() if f.last_scanned_at else None,
        )
        for f, is_online in zip(folders, online, strict=True)
    ]


async def get_folder(db: AsyncSession, folder_id: int) -> FolderView | None:
    return next((f for f in await list_folders(db) if f.id == folder_id), None)


async def add_folder(db: AsyncSession, raw_path: str, label: str | None = None) -> FolderView:
    path = await asyncio.to_thread(_validate_path, raw_path)
    existing = (await db.scalars(select(CustomArtFolder))).all()
    for folder in existing:
        if _overlaps(path, Path(folder.path)):
            if path == Path(folder.path):
                raise FolderRejected("already_linked", "Esa carpeta ya está vinculada.")
            raise FolderRejected(
                "overlaps",
                f"Se solapa con otra carpeta vinculada: {folder.path}",
            )
    if len(existing) >= MAX_LINKED_FOLDERS:
        raise FolderRejected("too_many", f"Como mucho {MAX_LINKED_FOLDERS} carpetas vinculadas.")

    folder = CustomArtFolder(path=str(path), label=(label or "").strip()[:256] or path.name)
    db.add(folder)
    await db.commit()
    await custom_art.rescan(db)
    log.info("Carpeta de arte custom vinculada: %s", path)
    view = await get_folder(db, folder.id)
    assert view is not None
    return view


async def folder_art_ids(db: AsyncSession, folder_id: int) -> list[int]:
    prefix = custom_art.linked_relative_path(folder_id, "")
    return list(
        (
            await db.scalars(
                select(CustomArt.id).where(
                    CustomArt.relative_path.startswith(prefix, autoescape=True)
                )
            )
        ).all()
    )


async def remove_folder(db: AsyncSession, folder_id: int) -> int | None:
    """Desvincula una carpeta. Devuelve cuántas imágenes salen del índice.

    Las cartas que usaban esas imágenes vuelven a su arte oficial; los
    ficheros siguen en su sitio.
    """
    folder = await db.get(CustomArtFolder, folder_id)
    if folder is None:
        return None
    art_ids = await folder_art_ids(db, folder_id)
    await custom_art.delete_arts(db, art_ids)
    await db.delete(folder)
    await db.commit()
    await custom_art.load_linked_roots(db)
    log.info("Carpeta desvinculada: %s (%d imágenes)", folder.path, len(art_ids))
    return len(art_ids)
