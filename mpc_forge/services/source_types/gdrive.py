"""``GDriveSourceType``: wrapper delgado sobre el indexer de Google Drive.

La lógica pesada (API v3 + fallback scraping, paginación de carpetas, batching)
vive en ``mpc_forge.services.gdrive_indexer``. Este archivo solo la expone
bajo la interfaz común ``ArtSourceType`` para que el indexer genérico pueda
dispatchar por tipo sin ``if source_type == 'gdrive'``.

Delegamos así en vez de mover el código para minimizar riesgo — el indexer
de Google Drive es funcional y su interfaz interna es específica de Drive
(usa file_id + folder recursion + Drive API v3). Mover todo eso a
``list_files()`` genérico sería un refactor grande sin ganancia inmediata.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from mpc_forge.models import ArtSource

from .base import ArtSourceType, SourceFile


class GDriveSourceType(ArtSourceType):
    """Google Drive carpetas (públicas o compartidas con API key).

    URLs aceptadas:
      - ``https://drive.google.com/drive/folders/{ID}``
      - ``https://drive.google.com/file/d/{ID}``   (source_type "gdrive-file")

    Delega al indexer específico de Google Drive; NO usa el path genérico
    ``list_files → SourceFile`` porque el indexer de Drive necesita cosas
    específicas (rate limiting, semaforado global, batching por ~1000 files
    contra API v3, y fallback a scraping HTML sin API key).
    """
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
        # No usado — la ruta de indexado de Drive es específica y vive en
        # gdrive_indexer.reindex_source_by_id(). El dispatcher en el módulo
        # `indexer` la llama directamente cuando source_type == "gdrive".
        raise NotImplementedError(
            "GDrive usa su propia ruta de indexado en gdrive_indexer, no la genérica"
        )
        yield  # unreachable


class GDriveFileSourceType(GDriveSourceType, register=True):
    """Un solo archivo de Google Drive (link `/file/d/{ID}`). No se indexa —
    solo se usa para descargar. Existe como tipo para diferenciarlo del
    folder y evitar que el indexer intente listar sus files.
    """
    key: ClassVar[str] = "gdrive-file"
    label: ClassVar[str] = "Google Drive (archivo)"

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        # Un archivo suelto no se indexa como fuente. Devolvemos vacío.
        return
        yield  # unreachable
