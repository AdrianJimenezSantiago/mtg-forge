"""Registro de tipos de source de arte.

Importar el paquete tiene efecto lateral: cada subclase de ``ArtSourceType``
se registra automáticamente en `base._REGISTRY` vía `__init_subclass__`. Los
consumidores usan ``resolve(source_type)`` para obtener la clase adecuada.

Ejemplo::

    from mpc_forge.services.source_types import resolve
    src_type_cls = resolve(source.source_type)   # → GDriveSourceType, etc.
    async for file in src_type_cls.list_files(source):
        ...
"""
from .base import (
    ArtSourceType,
    ArtSourceTypeError,
    IndexProgress,
    SourceFile,
    list_registered,
    resolve,
)
# Los imports siguientes solo existen para provocar el side-effect del
# __init_subclass__ que rellena el registry. No se re-exportan.
from .gdrive import GDriveSourceType, GDriveFileSourceType  # noqa: F401
from .http import HTTPListingSourceType  # noqa: F401
from .local_folder import LocalFolderSourceType  # noqa: F401
from .s3 import S3SourceType  # noqa: F401

__all__ = [
    "ArtSourceType",
    "ArtSourceTypeError",
    "IndexProgress",
    "SourceFile",
    "list_registered",
    "resolve",
]
