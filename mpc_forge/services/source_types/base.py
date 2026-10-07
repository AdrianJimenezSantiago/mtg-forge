from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar

from mpc_forge.models import ArtSource

_REGISTRY: dict[str, type[ArtSourceType]] = {}


@dataclass
class SourceFile:
    file_id: str
    filename: str
    folder_path: str = ""
    size_bytes: int = 0
    mime_type: str = ""


@dataclass
class IndexProgress:
    files_seen: int = 0
    folders_visited: int = 0
    warnings: list[str] = field(default_factory=list)


class ArtSourceTypeError(RuntimeError):
    pass


class ArtSourceType:
    key: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init_subclass__(cls, /, register: bool = True, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if not register:
            return
        if not cls.key:
            raise TypeError(f"ArtSourceType subclass {cls.__name__} lacks `key`")
        if not cls.label:
            raise TypeError(f"ArtSourceType subclass {cls.__name__} lacks `label`")
        _REGISTRY[cls.key] = cls

    @classmethod
    def validate_url(cls, url: str) -> str:
        return url.strip()

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        raise NotImplementedError

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        raise NotImplementedError

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        raise NotImplementedError
        yield


def resolve(source_type: str) -> type[ArtSourceType] | None:
    return _REGISTRY.get(source_type)


def list_registered() -> list[tuple[str, str]]:
    return [(cls.key, cls.label) for cls in _REGISTRY.values()]
