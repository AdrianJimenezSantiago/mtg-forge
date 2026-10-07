from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from mpc_forge.models import ArtSource

from .base import ArtSourceType, SourceFile


class GDriveSourceType(ArtSourceType):
    key: ClassVar[str] = "gdrive"
    label: ClassVar[str] = "Google Drive"

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        return f"https://drive.google.com/uc?id={file_id}&export=download"

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=w400"

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        raise NotImplementedError(
            "GDrive usa su propia ruta de indexado en gdrive_indexer, no la genérica"
        )
        yield


class GDriveFileSourceType(GDriveSourceType, register=True):
    key: ClassVar[str] = "gdrive-file"
    label: ClassVar[str] = "Google Drive (archivo)"

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        return
        yield
