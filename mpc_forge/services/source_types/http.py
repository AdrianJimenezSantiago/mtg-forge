"""``HTTPListingSourceType``: source servido por un JSON manifest en HTTP.

Uso típico: una GitHub Action semanal genera un ``sources.json`` con la lista
consolidada de los N drives conocidos, y el usuario lo añade como source
único. Cada refresh del listing baja el JSON, no re-scrapea Drive.

Formato JSON esperado::

    {
      "name": "MPCFill community pack",
      "version": "2026-08-01",
      "files": [
        {
          "file_id": "abc123",
          "filename": "Lightning Bolt [LEA 161].png",
          "folder_path": "Lightning Bolt/",
          "url": "https://cdn.example.com/xyz.png",
          "thumb_url": "https://cdn.example.com/xyz-thumb.jpg",
          "size_bytes": 234567,
          "mime_type": "image/png"
        }
      ]
    }

Los campos opcionales:
- ``thumb_url``: si falta, se usa la misma ``url`` como thumbnail.
- ``folder_path``: default "".
- ``size_bytes`` / ``mime_type``: para stats, no críticos.

file_id
-------
El manifest DEBE proveer ``file_id`` estable — se usa como PK dentro del
source y para dedupe entre re-indexados. Si el generador del JSON no tiene
IDs propios, puede usar un hash de la URL o el path.

Guardamos las URLs en un JSON blob dentro de ``IndexedArt.filename``... NO,
mejor: guardamos las URLs en ``IndexedArt.mime_type`` (repurposeado como
JSON) — hack feo. **Solución limpia**: añadir columnas ``download_url`` y
``thumb_url`` opcionales a IndexedArt. Ver TODO.

Por ahora, primera iteración: el `file_id` codifica la URL completa (base64)
y los helpers ``download_url()`` / ``thumbnail_url()`` la reconstruyen. Es
frágil (URLs largas rompen el índice) pero funcional para el MVP.

TODO (P2): añadir columnas ``download_url`` y ``thumb_url`` en IndexedArt
para desacoplar del file_id. Ver `mpc_forge/services/source_types/http.py`.
"""
from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource
from mpc_forge.ssl_config import ssl_insecure

from .base import ArtSourceType, ArtSourceTypeError, SourceFile

log = logging.getLogger(__name__)


def _encode_url(url: str) -> str:
    """Codifica una URL a un file_id URL-safe base64."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_url(file_id: str) -> str:
    padding = "=" * (-len(file_id) % 4)
    return base64.urlsafe_b64decode(file_id + padding).decode("utf-8")


class HTTPListingSourceType(ArtSourceType):
    """Manifiesto HTTP con lista de archivos remotos.

    Config:
      - ``ArtSource.url`` = URL del manifest JSON (debe devolver 200 + JSON válido).
    """
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
        # En esta primera iteración no distinguimos thumb del original —
        # devolvemos la URL completa. Si el manifest tiene thumb_urls
        # distintos y los queremos preservar, requiere columna extra en
        # IndexedArt (ver TODO en el docstring del módulo).
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
                return resp.json()
            except ValueError as e:
                raise ArtSourceTypeError(
                    f"El manifest de {url} no es JSON válido: {e}"
                ) from e

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
            # Preferimos el file_id explícito; si falta, derivamos de la URL.
            fid = entry.get("file_id") or _encode_url(str(url))
            yield SourceFile(
                file_id=str(fid)[:128],  # respeta el límite de la columna
                filename=str(filename),
                folder_path=str(entry.get("folder_path") or ""),
                size_bytes=int(entry.get("size_bytes") or 0),
                mime_type=str(entry.get("mime_type") or ""),
            )
