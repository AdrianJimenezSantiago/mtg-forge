"""Novedades de la versión instalada, una sola vez después de actualizar.

La primera vez que arranca una instalación no se enseña nada: solo se apunta
la versión. Las notas salen de la release de GitHub de esa versión; sin red
queda el enlace a la página de releases.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import __version__
from mpc_forge.models import KeyValue
from mpc_forge.ssl_config import ssl_insecure
from mpc_forge.updater import REPO

log = logging.getLogger(__name__)

KEY = "ui.last_seen_version"
RELEASES_URL = f"https://github.com/{REPO}/releases"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/tags/v{{version}}"
MAX_NOTES = 20_000
TIMEOUT = 4.0


def is_release(version: str = __version__) -> bool:
    return version not in ("", "0.0.0")


async def _get(db: AsyncSession) -> str | None:
    row = await db.get(KeyValue, KEY)
    return row.value if row else None


async def mark_seen(db: AsyncSession, version: str = __version__) -> None:
    row = await db.get(KeyValue, KEY)
    if row is None:
        db.add(KeyValue(key=KEY, value=version))
    else:
        row.value = version
    await db.commit()


async def fetch_notes(version: str, client: httpx.AsyncClient | None = None) -> str | None:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, verify=not ssl_insecure())
    try:
        resp = await client.get(
            RELEASE_API.format(version=version),
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"MPC-Forge/{version}"},
        )
        if resp.status_code != 200:
            return None
        body = resp.json().get("body")
        return str(body)[:MAX_NOTES] if body else None
    except (httpx.HTTPError, ValueError):
        log.debug("No se pudieron leer las notas de la v%s", version, exc_info=True)
        return None
    finally:
        if owns:
            await client.aclose()


async def pending(db: AsyncSession, version: str = __version__) -> dict[str, Any] | None:
    if not is_release(version):
        return None
    seen = await _get(db)
    if seen is None:
        await mark_seen(db, version)
        return None
    if seen == version:
        return None
    return {
        "version": version,
        "previous": seen,
        "notes": await fetch_notes(version),
        "url": f"{RELEASES_URL}/tag/v{version}",
    }
