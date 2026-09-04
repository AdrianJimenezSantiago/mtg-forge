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
from mpc_forge.services.rate_limiter import AsyncRateLimiter
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

Face = Literal["front", "back"]
Prefer = Literal["png", "large", "normal"]

# CDN de Scryfall (cards.scryfall.io) tolera ~10 req/s antes de 429/403.
# Interpretado como "espaciado mínimo entre inicios de descarga": permite
# solapar respuestas y usar el ancho de banda real, respetando la cadencia.
_DOWNLOAD_INTERVAL = 0.11

# Concurrencia máxima de descargas simultáneas. Con 8 tenemos throughput
# real cerca de 1/_DOWNLOAD_INTERVAL sin machacar el CDN.
_DOWNLOAD_CONCURRENCY = 8


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
        self._limiter = AsyncRateLimiter(_DOWNLOAD_INTERVAL)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ensure(
        self,
        db: AsyncSession,
        scryfall_id: str,
        face: Face = "front",
        prefer: Prefer = "png",
    ) -> LocalArt | None:
        """Descarga (si hace falta) y devuelve el ``LocalArt`` para un par
        ``(scryfall_id, face)``. Commit implícito.

        Para procesar muchas cartas de una tacada usa :meth:`ensure_many`, que
        paraleliza descargas y hace un único commit final.
        """
        results = await self.ensure_many(db, [(scryfall_id, face)], prefer=prefer)
        return results.get((scryfall_id, face))

    async def ensure_many(
        self,
        db: AsyncSession,
        requests: list[tuple[str, Face]],
        *,
        prefer: Prefer = "png",
        concurrency: int = _DOWNLOAD_CONCURRENCY,
    ) -> dict[tuple[str, Face], LocalArt | None]:
        """Asegura que cada par ``(scryfall_id, face)`` tiene arte en disco.

        Estrategia:
          1. Lee de un tirón los ``LocalArt`` y ``PrintingCache`` existentes.
          2. Determina qué peticiones son cache-hit, cuáles no tienen printing,
             y cuáles requieren bajar bytes.
          3. Descarga en paralelo (con :class:`AsyncRateLimiter` para respetar
             la cadencia del CDN y un ``Semaphore`` para acotar concurrencia).
          4. Escribe archivos y ``LocalArt`` de forma secuencial (misma sesión
             async → no se puede paralelizar) y hace UN commit al final.

        Comparado con llamar ``ensure()`` en un bucle: ahorra ``2*N`` queries
        de lookup, ``N`` commits, y compone las descargas en paralelo.
        """
        if not requests:
            return {}

        results: dict[tuple[str, Face], LocalArt | None] = {}

        unique = list({(sfid, face) for sfid, face in requests})
        sfids = {sfid for sfid, _ in unique}

        existing_rows = (
            await db.scalars(
                select(LocalArt).where(LocalArt.scryfall_id.in_(sfids))
            )
        ).all()
        existing_by_key: dict[tuple[str, Face], LocalArt] = {
            (la.scryfall_id, la.face): la for la in existing_rows  # type: ignore[misc]
        }

        printings_by_id: dict[str, PrintingCache] = {
            p.scryfall_id: p
            for p in (
                await db.scalars(
                    select(PrintingCache).where(PrintingCache.scryfall_id.in_(sfids))
                )
            ).all()
        }

        needs_download: list[tuple[str, Face, str]] = []
        for sfid, face in unique:
            key = (sfid, face)
            existing = existing_by_key.get(key)
            if existing:
                file_path = PATHS.art_dir / existing.relative_path
                if file_path.exists():
                    results[key] = existing
                    continue
                log.warning(
                    "Cache miss en disco para %s (face=%s), re-descargando",
                    sfid, face,
                )
            printing = printings_by_id.get(sfid)
            if not printing:
                results[key] = None
                continue
            url = _pick_image_url(printing, face, prefer)
            if not url:
                results[key] = None
                continue
            needs_download.append((sfid, face, url))

        if not needs_download:
            return results

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def _fetch(sfid: str, face: Face, url: str) -> tuple[str, Face, str, bytes | None]:
            async with semaphore:
                await self._limiter.acquire()
                try:
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    log.error("Error descargando %s: %s", url, e)
                    return sfid, face, url, None
                return sfid, face, url, resp.content

        downloads = await asyncio.gather(*(_fetch(*t) for t in needs_download))

        # Escritura de disco y BD: secuencial (aiosqlite serialize writes de
        # todos modos, y AsyncSession no soporta uso concurrente).
        dirty = False
        for sfid, face, url, data in downloads:
            key = (sfid, face)
            if data is None:
                results[key] = None
                continue
            digest = hashlib.sha256(data).hexdigest()

            dup = await db.scalar(select(LocalArt).where(LocalArt.sha256 == digest))
            existing = existing_by_key.get(key)

            if dup:
                rel = dup.relative_path
            else:
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
                results[key] = existing
            else:
                art = LocalArt(
                    sha256=digest,
                    relative_path=rel,
                    scryfall_id=sfid,
                    face=face,
                    bytes_size=len(data),
                )
                db.add(art)
                existing_by_key[key] = art
                results[key] = art
            dirty = True

        if dirty:
            await db.commit()

        return results

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
