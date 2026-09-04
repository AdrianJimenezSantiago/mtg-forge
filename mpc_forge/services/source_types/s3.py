"""``S3SourceType``: indexa un bucket S3 o Cloudflare R2 público.

Motivación
----------
Complementario a `HTTPListingSourceType`. Los usuarios comunitarios cada
vez más alojan sus artes en Cloudflare R2 (gratis para lectura) o buckets
S3 públicos, evitando la fragilidad de Google Drive y el rate-limit no
documentado. Este tipo lista objetos de un bucket público mediante la API
XML de S3 (que R2 también implementa) — sin credenciales.

URLs aceptadas
--------------
- ``s3://bucket-name/optional/prefix/``
- ``https://bucket-name.s3.amazonaws.com/optional/prefix/``
- ``https://bucket-name.s3.<region>.amazonaws.com/``
- ``https://<hash>.r2.cloudflarestorage.com/bucket-name/``  (R2 dev domain)
- ``https://cdn.example.com/`` con parámetro ``bucket=`` en la URL para R2
  con custom domain (heurística: si no detecta bucket, se pide explícito).

Formato del listing
-------------------
S3 devuelve XML con `<Contents><Key>...</Key><Size>...</Size></Contents>`
por objeto. Paginación via `ContinuationToken`. Máximo 1000 objetos por
llamada — para buckets grandes iteramos.

Descarga y thumbnails
---------------------
El URL de descarga es directo al objeto: `{bucket-endpoint}/{key}`. Como
S3/R2 no ofrece thumbnails on-the-fly (a diferencia de gdrive), la URL de
thumbnail es la misma que download — para buckets grandes convendría
redimensionar antes de subir (fuera del alcance de este tipo).

Se guardan URLs en ``IndexedArt.download_url`` / ``thumb_url`` para no
depender del ``file_id`` (que aquí es la key S3, potencialmente larga).

Sin credenciales
----------------
Este tipo asume bucket PÚBLICO (lectura anónima). Los buckets S3 privados
requieren AWS SDK + credenciales — fuera de alcance por complejidad y
porque los drives comunitarios de MTG son públicos por naturaleza.
"""
from __future__ import annotations

import logging
import re
from typing import AsyncIterator, ClassVar
from urllib.parse import urlparse

import httpx

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource
from mpc_forge.ssl_config import ssl_insecure

from .base import ArtSourceType, ArtSourceTypeError, SourceFile

log = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAX_KEYS = 1000  # Límite S3 por request
_MAX_PAGES = 100  # Salvaguarda contra buckets astronómicos (~100k objetos)


def _parse_s3_url(raw: str) -> tuple[str, str, str]:
    """Devuelve ``(bucket, prefix, endpoint)`` a partir de una URL.

    Reconoce:
      - ``s3://bucket/prefix``            → ("bucket", "prefix", https://bucket.s3.amazonaws.com)
      - ``https://bucket.s3.amazonaws.com/prefix`` → ("bucket", "prefix", https://bucket.s3.amazonaws.com)
      - ``https://<hash>.r2.cloudflarestorage.com/bucket/prefix`` → ("bucket", "prefix", <endpoint>)

    Levanta ValueError si no puede parsear.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL vacía")

    # Formato s3://
    if raw.startswith("s3://"):
        rest = raw[5:]
        parts = rest.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        if not bucket:
            raise ValueError("bucket vacío en s3://")
        return bucket, prefix.strip("/"), f"https://{bucket}.s3.amazonaws.com"

    # Formato HTTPS
    p = urlparse(raw)
    if p.scheme not in {"http", "https"}:
        raise ValueError(f"Scheme no soportado: {p.scheme}")
    if not p.netloc:
        raise ValueError(f"URL inválida: {raw}")

    host = p.netloc.lower()
    path = p.path or "/"

    # Virtual-hosted: bucket.s3.amazonaws.com o bucket.s3.<region>.amazonaws.com
    m = re.match(r"^([a-z0-9][a-z0-9.\-]*[a-z0-9])\.s3(?:[.\-][a-z0-9-]+)?\.amazonaws\.com$", host)
    if m:
        bucket = m.group(1)
        prefix = path.lstrip("/").strip("/")
        return bucket, prefix, f"{p.scheme}://{host}"

    # Path-style: s3.amazonaws.com/bucket/prefix
    if host in ("s3.amazonaws.com",) or host.startswith("s3."):
        segs = [s for s in path.split("/") if s]
        if not segs:
            raise ValueError("Falta el bucket en la path")
        bucket = segs[0]
        prefix = "/".join(segs[1:])
        return bucket, prefix, f"{p.scheme}://{host}/{bucket}"

    # Cloudflare R2 dev domain
    if host.endswith(".r2.cloudflarestorage.com") or host.endswith(".r2.dev"):
        segs = [s for s in path.split("/") if s]
        if not segs:
            raise ValueError("Falta el bucket en la path")
        bucket = segs[0]
        prefix = "/".join(segs[1:])
        return bucket, prefix, f"{p.scheme}://{host}/{bucket}"

    raise ValueError(
        "URL no reconocida como S3/R2. Formatos aceptados: "
        "'s3://bucket/prefix', 'https://bucket.s3.amazonaws.com/', "
        "'https://<hash>.r2.cloudflarestorage.com/bucket/'."
    )


# Regex para parsear el XML de ListObjectsV2 sin dependencia de lxml.
# Extrae Key y Size de cada <Contents>. Suficiente para respuestas
# well-formed de AWS/R2; para adversariales convendría ElementTree.
_S3_CONTENTS_RE = re.compile(
    r"<Contents>.*?<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>.*?</Contents>",
    re.DOTALL,
)
_S3_NEXT_TOKEN_RE = re.compile(r"<NextContinuationToken>([^<]+)</NextContinuationToken>")
_S3_TRUNCATED_RE = re.compile(r"<IsTruncated>(true|false)</IsTruncated>", re.IGNORECASE)


class S3SourceType(ArtSourceType):
    """Bucket S3 o Cloudflare R2 público."""

    key: ClassVar[str] = "s3"
    label: ClassVar[str] = "S3 / Cloudflare R2 (público)"

    request_timeout: ClassVar[float] = 30.0

    @classmethod
    def validate_url(cls, url: str) -> str:
        # Aprovechamos _parse_s3_url para la validación completa.
        bucket, prefix, endpoint = _parse_s3_url(url)
        # Devolver una forma canónica s3:// para almacenamiento consistente.
        if prefix:
            return f"s3://{bucket}/{prefix}"
        return f"s3://{bucket}"

    @classmethod
    def download_url(cls, source: ArtSource, file_id: str) -> str:
        """URL directa al objeto. `file_id` es la key S3 (path completo dentro
        del bucket). Como puede tener slashes, devolvemos el URL sin
        codificación adicional — la key ya viene URL-safe del listing.
        """
        try:
            bucket, _, endpoint = _parse_s3_url(source.url)
        except ValueError:
            return ""
        # Si el endpoint es virtual-hosted, no incluir bucket en el path.
        if "amazonaws.com" in endpoint and f"{bucket}." in endpoint:
            return f"{endpoint}/{file_id}"
        return f"{endpoint}/{file_id}"

    @classmethod
    def thumbnail_url(cls, source: ArtSource, file_id: str) -> str:
        # Sin transformación server-side. R2/S3 no ofrece resize on-the-fly.
        # Los usuarios que quieran thumbnails deben pre-generarlos o servir
        # tras Cloudflare Images (fuera de este tipo).
        return cls.download_url(source, file_id)

    @classmethod
    async def _list_page(
        cls, client: httpx.AsyncClient, endpoint: str, prefix: str,
        continuation_token: str | None,
    ) -> tuple[list[tuple[str, int]], str | None]:
        """Devuelve ``(entries, next_token)`` para una página de ListObjectsV2.

        `entries` es una lista de tuplas ``(key, size)``. `next_token` es
        None si no hay más páginas.
        """
        params: dict[str, str] = {
            "list-type": "2",
            "max-keys": str(_MAX_KEYS),
        }
        if prefix:
            params["prefix"] = prefix
        if continuation_token:
            params["continuation-token"] = continuation_token

        # El endpoint tiene la forma https://bucket.s3.amazonaws.com — el
        # listing va al root del bucket.
        resp = await client.get(endpoint, params=params)
        resp.raise_for_status()
        body = resp.text

        entries: list[tuple[str, int]] = []
        for m in _S3_CONTENTS_RE.finditer(body):
            key = m.group(1)
            try:
                size = int(m.group(2))
            except ValueError:
                size = 0
            entries.append((key, size))

        truncated_m = _S3_TRUNCATED_RE.search(body)
        is_truncated = truncated_m and truncated_m.group(1).lower() == "true"
        next_token = None
        if is_truncated:
            token_m = _S3_NEXT_TOKEN_RE.search(body)
            if token_m:
                next_token = token_m.group(1)
        return entries, next_token

    @classmethod
    async def list_files(cls, source: ArtSource) -> AsyncIterator[SourceFile]:
        try:
            bucket, prefix, endpoint = _parse_s3_url(source.url)
        except ValueError as e:
            raise ArtSourceTypeError(f"URL S3 inválida en source '{source.name}': {e}") from e

        async with httpx.AsyncClient(
            timeout=cls.request_timeout,
            follow_redirects=True,
            verify=not ssl_insecure(),
            headers={"User-Agent": cfg.MOXFIELD_USER_AGENT},
        ) as client:
            continuation_token: str | None = None
            for page_idx in range(_MAX_PAGES):
                try:
                    entries, next_token = await cls._list_page(
                        client, endpoint, prefix, continuation_token,
                    )
                except httpx.HTTPStatusError as e:
                    raise ArtSourceTypeError(
                        f"Error listando bucket {bucket!r} ({e.response.status_code}): "
                        f"{e.response.text[:200]}"
                    ) from e

                for key, size in entries:
                    # Filtro por extensión (imágenes solamente)
                    ext_start = key.rfind(".")
                    ext = key[ext_start:].lower() if ext_start > 0 else ""
                    if ext not in _IMAGE_EXTENSIONS:
                        continue

                    # Filename = último segmento; folder_path = todo lo previo.
                    slash = key.rfind("/")
                    if slash > 0:
                        folder_path = key[:slash]
                        filename = key[slash + 1:]
                    else:
                        folder_path = ""
                        filename = key

                    yield SourceFile(
                        file_id=key[:128],  # respeta el límite String(128)
                        filename=filename,
                        folder_path=folder_path,
                        size_bytes=size,
                        mime_type=f"image/{ext.lstrip('.').replace('jpg', 'jpeg')}",
                    )

                if not next_token:
                    break
                continuation_token = next_token

            if page_idx == _MAX_PAGES - 1:
                log.warning(
                    "S3 listing en source %d alcanzó el límite de %d páginas "
                    "(%d objetos como máximo). Es posible que falten archivos.",
                    source.id, _MAX_PAGES, _MAX_PAGES * _MAX_KEYS,
                )
