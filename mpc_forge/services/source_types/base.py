"""``ArtSourceType``: abstracción sobre "de dónde vienen los artes indexados".

Motivación
----------
Hasta la Fase 2, `IndexedArt` asumía que su source era siempre un Google Drive
(URL de thumb/download codificada en `gdrive_search`). Esto impedía:

- Indexar una carpeta local (útil para escaneos personales o para usar la app
  sin conexión).
- Indexar un HTTP listing (JSON manifest o índice generado por un tercero,
  ej. una GitHub Action semanal que agrega drives conocidos).
- Indexar un S3/R2 bucket (comunidades que hospedan en Cloudflare).

Diseño
------
Cada tipo de source implementa esta clase abstracta. La instancia del tipo
se resuelve a partir de ``ArtSource.source_type`` mediante ``resolve()``.

Cada tipo debe implementar:

- ``list_files(source)`` — devuelve un ``AsyncIterator[SourceFile]`` con los
  archivos disponibles. La ejecución es responsabilidad del tipo (paginación
  API, recursión de carpetas, HTTP GET…).
- ``download_url(source, file_id)`` — URL directa para descargar el archivo.
- ``thumbnail_url(source, file_id)`` — URL de thumbnail (para el picker).

Los tipos NO manejan la persistencia: el ``indexer`` global se encarga de
recibir los ``SourceFile`` iterados y hacer upsert en la BD. Esto mantiene
la lógica común (extract_tags, normalización, batching, commits parciales)
en un solo sitio.

Ver ``gdrive.py`` como referencia canónica.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar

from mpc_forge.models import ArtSource

# Registry poblado por ``__init_subclass__`` en cada subclase.
_REGISTRY: dict[str, type[ArtSourceType]] = {}


@dataclass
class SourceFile:
    """Un archivo descubierto durante el listado. Estructura común que el
    indexer sabe cómo persistir independientemente del tipo de source.
    """
    file_id: str            # identificador único DENTRO del source (path o gdrive file id)
    filename: str           # nombre visible al usuario (incluida extensión)
    folder_path: str = ""   # subruta relativa dentro del source (para tags jerárquicos)
    size_bytes: int = 0
    mime_type: str = ""


@dataclass
class IndexProgress:
    """Estado incremental que emite el listado. El indexer lo usa para
    mostrar progreso en la UI (barra "234/500 files, 3 folders visited").
    """
    files_seen: int = 0
    folders_visited: int = 0
    # Errores no fatales encontrados (ej. una subcarpeta que devolvió 403).
    # No abortan el indexado, pero se reportan al final.
    warnings: list[str] = field(default_factory=list)


class ArtSourceTypeError(RuntimeError):
    """Error genérico al operar con un source (URL mala, permisos, etc.)."""


class ArtSourceType:
    """Base abstracta para tipos de source de arte.

    Subclases DEBEN definir:
      - ``key``: identificador estable (guardado en ``ArtSource.source_type``)
      - ``label``: nombre legible para la UI ("Google Drive", "Local folder")

    Subclases DEBEN implementar:
      - ``async def list_files(source) -> AsyncIterator[SourceFile]``
      - ``def download_url(source, file_id) -> str``
      - ``def thumbnail_url(source, file_id) -> str``

    Opcional:
      - ``validate_url(url) -> str`` — normaliza y valida la URL al añadir.
        Por defecto devuelve la URL tal cual; sobreescribir para validaciones
        específicas (ej. rechazar URLs que no sean carpetas de Google Drive).
    """

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

    # --- API opcional -------------------------------------------------------
    @classmethod
    def validate_url(cls, url: str) -> str:
        """Normaliza y valida una URL para este tipo. Lanza ValueError si es
        inválida. Devuelve la URL canónica.
        """
        return url.strip()

    # --- API obligatoria ---------------------------------------------------
    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        raise NotImplementedError

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        raise NotImplementedError

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        """Yielda un ``SourceFile`` por cada archivo descubierto. El indexer
        recibe cada uno y lo procesa (normaliza + tags + canonical + upsert).

        Puede lanzar excepciones si el source es inaccesible. El indexer las
        captura y las registra en `ArtSource.index_error` para mostrar al
        usuario.

        NOTA: es un `async generator` — implementaciones concretas deben usar
        ``async def list_files(...)`` con ``yield``.
        """
        raise NotImplementedError
        yield  # unreachable — para type check


def resolve(source_type: str) -> type[ArtSourceType] | None:
    """Devuelve la clase para ``source_type`` o None si no está registrada."""
    return _REGISTRY.get(source_type)


def list_registered() -> list[tuple[str, str]]:
    """Devuelve ``[(key, label), …]`` de todos los tipos registrados. Útil para
    poblar un `<select>` de tipo de source en la UI.
    """
    return [(cls.key, cls.label) for cls in _REGISTRY.values()]
