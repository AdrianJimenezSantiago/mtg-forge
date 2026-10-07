from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar, cast
from urllib.parse import urlparse

import httpx

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource
from mpc_forge.ssl_config import ssl_insecure

from .base import ArtSourceType, ArtSourceTypeError, SourceFile

log = logging.getLogger(__name__)


def _encode_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_url(file_id: str) -> str:
    padding = "=" * (-len(file_id) % 4)
    return base64.urlsafe_b64decode(file_id + padding).decode("utf-8")


class HTTPListingSourceType(ArtSourceType):
    key: ClassVar[str] = "http-listing"
    label: ClassVar[str] = "HTTP manifest"

    request_timeout: ClassVar[float] = 30.0

    @classmethod
    def validate_url(cls, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            raise ValueError("La URL del manifest no puede estar vacía")
        p = urlparse(raw)
        if p.scheme not in {"http", "https"}:
            raise ValueError("La URL debe empezar por http:// o https://")
        if not p.netloc:
            raise ValueError(f"URL inválida: {raw}")
        return raw

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        try:
            return _decode_url(file_id)
        except Exception:
            return ""

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        return cls.download_url(source, file_id)

    @classmethod
    async def _fetch_manifest(cls, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=cls.request_timeout,
            follow_redirects=True,
            verify=not ssl_insecure(),
            headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
        ) as client:
            log.debug("[http-listing] GET %s", url)
            resp = await client.get(url)
            resp.raise_for_status()
            try:
                return cast(dict[str, Any], resp.json())
            except ValueError as e:
                raise ArtSourceTypeError(f"El manifest de {url} no es JSON válido: {e}") from e

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        manifest = await cls._fetch_manifest(source.url)
        files = manifest.get("files") or []
        if not isinstance(files, list):
            raise ArtSourceTypeError(
                f"El manifest debe tener una clave 'files' con array — got {type(files).__name__}"
            )
        for entry in files:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            filename = entry.get("filename")
            if not url or not filename:
                continue
            fid = entry.get("file_id") or _encode_url(str(url))
            yield SourceFile(
                file_id=str(fid)[:128],
                filename=str(filename),
                folder_path=str(entry.get("folder_path") or ""),
                size_bytes=int(entry.get("size_bytes") or 0),
                mime_type=str(entry.get("mime_type") or ""),
            )
