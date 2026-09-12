"""``LocalFolderSourceType``: indexa una carpeta del sistema de archivos.

Uso típico:
- El usuario tiene una colección organizada en disco (ej. `D:/mtg-art/`) y
  quiere buscarla desde el picker igual que un drive.
- Un NAS compartido en la LAN (``\\\\SERVER\\mtg-art\\``) que varios usuarios
  quieren usar sin subir a Drive.
- La carpeta `custom_art/_downloaded/` de la propia app, si se quiere
  poder buscarla como source más (útil para debug).

URL
---
Guardamos la ruta absoluta como URL (con prefijo ``file://`` opcional para
distinguirla visualmente en la UI). Al indexar la resolvemos vía ``pathlib``.

Descarga y thumbnails
---------------------
Devolvemos URLs relativas ``/local-source/{source_id}/{file_id}`` — el
``file_id`` es la ruta relativa dentro del source (URL-safe base64).
Registrar la ruta HTTP ``/local-source/…`` es responsabilidad del setup en
``app.py`` (fuera del alcance de esta Tarea 7 — ver TODO).

TODO (P2): añadir la ruta HTTP que sirve estos ficheros con `FileResponse`,
para que los thumbnails funcionen en el picker igual que con Drive.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

from mpc_forge.models import ArtSource

from .base import ArtSourceType, ArtSourceTypeError, SourceFile

log = logging.getLogger(__name__)

# Extensiones aceptadas — mismo criterio que el gdrive_indexer.
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Ficheros que se recogen por cada salto al hilo de I/O. Bastante grande para
# que el coste del cambio de contexto sea despreciable, bastante pequeño para
# que el event loop no se quede sin atender más de unos milisegundos.
_SCAN_BATCH = 200


def _encode_relpath(relpath: str) -> str:
    """Codifica una ruta relativa a un file_id URL-safe.

    Usamos base64 sin padding para que sirva como file_id en la BD (columna
    String(128)) y en URLs sin escapes. La ruta puede tener slashes y
    caracteres unicode, base64 lo aplana.
    """
    return base64.urlsafe_b64encode(relpath.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_relpath(file_id: str) -> str:
    """Inverso de ``_encode_relpath`` para reconstruir la ruta al descargar."""
    padding = "=" * (-len(file_id) % 4)
    return base64.urlsafe_b64decode(file_id + padding).decode("utf-8")


class LocalFolderSourceType(ArtSourceType):
    """Carpeta local del sistema de archivos.

    Config:
      - ``ArtSource.url`` = ruta absoluta (con o sin prefijo ``file://``).

    Recorrido:
      - Recursivo por defecto.
      - Ignora ficheros ocultos (empiezan por ``.``) y carpetas comunes de
        sistema (``__pycache__``, ``.git``, ``node_modules``, ``$RECYCLE.BIN``).
      - Solo imágenes según ``_IMAGE_EXTENSIONS``.
    """
    key: ClassVar[str] = "local-folder"
    label: ClassVar[str] = "Carpeta local"

    _IGNORE_DIRS: ClassVar[frozenset[str]] = frozenset({
        "__pycache__", ".git", ".hg", ".svn", "node_modules",
        "$RECYCLE.BIN", "System Volume Information", ".DS_Store",
    })

    @classmethod
    def validate_url(cls, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            raise ValueError("La ruta no puede estar vacía")
        # Aceptamos file:// como prefijo por comodidad al copiar del explorador.
        if raw.startswith("file://"):
            raw = raw[7:]
            # Windows: file:///C:/… → /C:/… → C:/…
            if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
                raw = raw[1:]
        path = Path(raw).expanduser()
        if not path.exists():
            raise ValueError(f"La ruta no existe: {path}")
        if not path.is_dir():
            raise ValueError(f"La ruta no es un directorio: {path}")
        # Devolvemos la ruta absoluta resuelta como URL canónica.
        return str(path.resolve())

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        # Servida por la ruta HTTP `/local-source/{source_id}/{file_id}` que
        # se registrará como TODO. Por ahora devolvemos ese path relativo
        # y el frontend lo trata como URL absoluta contra el host actual.
        return f"/local-source/{source.id}/{file_id}"

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        # Sin transformación de tamaño servidor por ahora — devolvemos el
        # fichero completo. Para índices muy grandes convendría un endpoint
        # que sirva un thumbnail redimensionado on-the-fly (Pillow).
        # Ver TODO Fase 2/3 pHash.
        return cls.download_url(source, file_id)

    @classmethod
    def resolve_path(cls, source: ArtSource, file_id: str) -> Path:
        """Reconstruye la ruta absoluta en disco para un ``file_id`` dado.

        Verifica que la ruta resultante SIGUE dentro de la carpeta del source
        (defensa contra path traversal). Lanza ``ArtSourceTypeError`` si no.
        """
        base = Path(source.url).resolve()
        try:
            relpath = _decode_relpath(file_id)
        except Exception as e:
            raise ArtSourceTypeError(f"file_id inválido: {e}") from e
        candidate = (base / relpath).resolve()
        # Path traversal defense: candidate.is_relative_to(base) requiere 3.9+.
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
            raise ArtSourceTypeError(
                f"La carpeta del source '{source.name}' no existe: {base}"
            )

        # `rglob` y los `stat()` que lleva detrás son I/O síncrona: sobre una
        # carpeta grande (un NAS con 50k imágenes, o peor, uno montado por
        # red) bloquean el event loop varios segundos. Durante ese rato la app
        # entera deja de responder, incluido el endpoint de progreso que la UI
        # consulta para pintar la barra del indexado.
        #
        # Se recorre por lotes en un hilo: el escaneo avanza fuera del loop y
        # entre lote y lote el loop recupera el control para atender
        # peticiones. El generador sigue siendo perezoso, así que la memoria no
        # crece con el tamaño de la carpeta.
        def _scan_batch(iterator, size: int) -> list[SourceFile]:
            out: list[SourceFile] = []
            for entry in iterator:
                # Filtrar dirs ignoradas — chequeo por segmentos del path.
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
                out.append(SourceFile(
                    file_id=_encode_relpath(relpath),
                    filename=entry.name,
                    folder_path=folder_path,
                    size_bytes=size,
                    mime_type=(
                        f"image/{entry.suffix.lower().lstrip('.').replace('jpg', 'jpeg')}"
                    ),
                ))
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
