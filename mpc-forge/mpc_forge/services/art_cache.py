"""Descarga y cachea artes de Scryfall a disco, con dedupe por SHA256.

Regla de oro: NUNCA hay dos archivos con el mismo contenido en `art_dir`.
Si dos scryfall_ids devuelven bytes idénticos, ambos apuntan al mismo LocalArt.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Literal

import aiofiles
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.config import PATHS, SCRYFALL_USER_AGENT
from mpc_forge.models import LocalArt, PrintingCache
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

Face = Literal["front", "back"]

# Rate limit: el CDN de Scryfall (cards.scryfall.io) tolera ~10 req/s.
# Un sleep suave entre descargas evita 429/403 en tandas de 100+ cartas.
_DOWNLOAD_SLEEP = 0.11


class ArtCache:
    """Descargador y dedupe de imágenes."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # IMPORTANTE: `cards.scryfall.io` bloquea User-Agents por defecto de
        # httpx / requests con 403. Debemos identificarnos explícitamente.
        self._client = client or httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            verify=not ssl_insecure(),
            headers={
                "User-Agent": SCRYFALL_USER_AGENT,
                "Accept": "image/png,image/jpeg,image/webp,image/*,*/*;q=0.8",
            },
        )
        self._download_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ensure(
        self,
        db: AsyncSession,
        scryfall_id: str,
        face: Face = "front",
        prefer: Literal["png", "large", "normal"] = "png",
    ) -> LocalArt | None:
        """Se asegura de que la imagen está en disco y devuelve el LocalArt.

        Si ya está cacheada (por scryfall_id + face) no vuelve a descargar.
        Si el hash coincide con otra descarga previa, reutiliza el archivo.
        """
        existing = await db.scalar(
            select(LocalArt).where(
                LocalArt.scryfall_id == scryfall_id, LocalArt.face == face
            )
        )
        if existing:
            file_path = PATHS.art_dir / existing.relative_path
            if file_path.exists():
                return existing
            log.warning("Cache miss en disco para %s (face=%s), rebajando", scryfall_id, face)

        printing = await db.get(PrintingCache, scryfall_id)
        if not printing:
            return None

        url = _pick_image_url(printing, face, prefer)
        if not url:
            return None

        try:
            async with self._download_lock:
                await asyncio.sleep(_DOWNLOAD_SLEEP)
                resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Error descargando %s: %s", url, e)
            return None

        data = resp.content
        digest = hashlib.sha256(data).hexdigest()

        # ¿Existe ya un archivo con este hash? → reutilizamos.
        dup = await db.scalar(select(LocalArt).where(LocalArt.sha256 == digest))
        if dup:
            if existing:
                existing.sha256 = digest
                existing.relative_path = dup.relative_path
                existing.bytes_size = len(data)
                await db.commit()
                return existing
            art = LocalArt(
                sha256=digest,
                relative_path=dup.relative_path,
                scryfall_id=scryfall_id,
                face=face,
                bytes_size=len(data),
            )
            db.add(art)
            await db.commit()
            return art

        # Nuevo: guardamos con nombre determinista basado en hash.
        ext = _extension_for(url)
        rel = _hash_relpath(digest, ext)
        abs_path = PATHS.art_dir / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(abs_path, "wb") as f:
            await f.write(data)

        if existing:
            existing.sha256 = digest
            existing.relative_path = rel
            existing.bytes_size = len(data)
            await db.commit()
            return existing

        art = LocalArt(
            sha256=digest,
            relative_path=rel,
            scryfall_id=scryfall_id,
            face=face,
            bytes_size=len(data),
        )
        db.add(art)
        await db.commit()
        return art

    def absolute_path(self, art: LocalArt) -> Path:
        return (PATHS.art_dir / art.relative_path).resolve()


def _pick_image_url(
    printing: PrintingCache,
    face: Face,
    prefer: Literal["png", "large", "normal"],
) -> str | None:
    if face == "back":
        candidates = [
            (prefer == "png", printing.back_image_png),
            (prefer in {"png", "large"}, printing.back_image_large),
            (True, printing.back_image_normal),
        ]
    else:
        candidates = [
            (prefer == "png", printing.image_png),
            (prefer in {"png", "large"}, printing.image_large),
            (True, printing.image_normal),
        ]
    for wanted, url in candidates:
        if wanted and url:
            return url
    return None


def _extension_for(url: str) -> str:
    lower = url.split("?", 1)[0].lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        if lower.endswith(ext):
            return ext
    return ".jpg"


def _hash_relpath(digest: str, ext: str) -> str:
    # Sharding en dos niveles para no meter miles de archivos en una carpeta.
    return f"{digest[:2]}/{digest[2:4]}/{digest}{ext}"
