from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import ClassVar

from mpc_forge.models import ArtSource

from .base import ArtSourceType, ArtSourceTypeError, SourceFile

log = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

_SCAN_BATCH = 200


def _encode_relpath(relpath: str) -> str:
    return base64.urlsafe_b64encode(relpath.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_relpath(file_id: str) -> str:
    padding = "=" * (-len(file_id) % 4)
    return base64.urlsafe_b64decode(file_id + padding).decode("utf-8")


class LocalFolderSourceType(ArtSourceType):
    key: ClassVar[str] = "local-folder"
    label: ClassVar[str] = "Carpeta local"

    _IGNORE_DIRS: ClassVar[frozenset[str]] = frozenset(
        {
            "__pycache__",
            ".git",
            ".hg",
            ".svn",
            "node_modules",
            "$RECYCLE.BIN",
            "System Volume Information",
            ".DS_Store",
        }
    )

    @classmethod
    def validate_url(cls, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            raise ValueError("La ruta no puede estar vacía")
        if raw.startswith("file://"):
            raw = raw[7:]
            if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]
        path = Path(raw).expanduser()
        if not path.exists():
            raise ValueError(f"La ruta no existe: {path}")
        if not path.is_dir():
            raise ValueError(f"La ruta no es un directorio: {path}")
        return str(path.resolve())

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        return f"/local-source/{source.id}/{file_id}"

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        return cls.download_url(source, file_id)

    @classmethod
    def resolve_path(cls, source: ArtSource, file_id: str) -> Path:
        base = Path(source.url).resolve()
        try:
            relpath = _decode_relpath(file_id)
        except Exception as e:
            raise ArtSourceTypeError(f"file_id inválido: {e}") from e
        candidate = (base / relpath).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as e:
            raise ArtSourceTypeError(
                f"file_id fuera del source (path traversal detectado): {relpath}"
            ) from e
        if not candidate.exists():
            raise ArtSourceTypeError(f"Fichero no encontrado: {candidate}")
        return candidate

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        base = Path(source.url).expanduser().resolve()
        if not base.is_dir():
            raise ArtSourceTypeError(f"La carpeta del source '{source.name}' no existe: {base}")

        def _scan_batch(iterator: Iterator[Path], size: int) -> list[SourceFile]:
            out: list[SourceFile] = []
            for entry in iterator:
                if any(seg in cls._IGNORE_DIRS for seg in entry.parts):
                    continue
                if entry.name.startswith("."):
                    continue
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in _IMAGE_EXTENSIONS:
                    continue

                relpath = entry.relative_to(base).as_posix()
                folder_path = "/".join(entry.relative_to(base).parts[:-1])
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                out.append(
                    SourceFile(
                        file_id=_encode_relpath(relpath),
                        filename=entry.name,
                        folder_path=folder_path,
                        size_bytes=size,
                        mime_type=(
                            f"image/{entry.suffix.lower().lstrip('.').replace('jpg', 'jpeg')}"
                        ),
                    )
                )
                if len(out) >= size_limit:
                    break
            return out

        size_limit = _SCAN_BATCH
        iterator = base.rglob("*")
        while True:
            batch = await asyncio.to_thread(_scan_batch, iterator, size_limit)
            if not batch:
                break
            for source_file in batch:
                yield source_file
