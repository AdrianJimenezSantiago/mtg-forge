from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import SCRYFALL_USER_AGENT
from mpc_forge.models import KeyValue
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

# La lista de sets se guarda en la BD para que la colección funcione sin red.
_SETS_KEY = "scryfall_sets.data"
_FETCHED_KEY = "scryfall_sets.fetched_at"
REFRESH_AFTER = timedelta(days=1)

_memo: tuple[datetime, list[dict[str, Any]]] | None = None

# Scryfall pide descargar los iconos y servirlos uno mismo en vez de enlazarlos.
_ICON_URI = re.compile(r"^https://svgs\.scryfall\.io/sets/([a-z0-9_-]+)\.svg(\?.*)?$")
_icon_locks: dict[str, asyncio.Lock] = {}
_client: httpx.AsyncClient | None = None
_prefetch: asyncio.Task[None] | None = None

_PREFETCH_CONCURRENCY = 8


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": SCRYFALL_USER_AGENT},
            timeout=20.0,
            verify=not ssl_insecure(),
        )
    return _client


async def aclose() -> None:
    global _client
    if _prefetch is not None and not _prefetch.done():
        _prefetch.cancel()
    if _client is not None:
        await _client.aclose()
        _client = None


def _slim(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": s.get("code", ""),
        "name": s.get("name", ""),
        "set_type": s.get("set_type", ""),
        "released_at": s.get("released_at"),
        "card_count": s.get("card_count", 0),
        "icon_svg_uri": s.get("icon_svg_uri"),
    }


async def all_sets(db: AsyncSession, scryfall: ScryfallClient) -> list[dict[str, Any]]:
    global _memo
    now = datetime.now(UTC)
    if _memo is not None and now - _memo[0] < REFRESH_AFTER:
        return _memo[1]

    stored = await db.get(KeyValue, _SETS_KEY)
    fetched = await db.get(KeyValue, _FETCHED_KEY)
    if stored and fetched and now - datetime.fromisoformat(fetched.value) < REFRESH_AFTER:
        _memo = (datetime.fromisoformat(fetched.value), json.loads(stored.value))
        return _memo[1]

    try:
        data = [_slim(s) for s in await scryfall.sets()]
    except Exception as e:
        if stored:
            log.warning("No se pudo actualizar la lista de sets; se usa la guardada: %s", e)
            return list(json.loads(stored.value))
        raise
    for key, value in ((_SETS_KEY, json.dumps(data)), (_FETCHED_KEY, now.isoformat())):
        row = await db.get(KeyValue, key)
        if row is None:
            db.add(KeyValue(key=key, value=value))
        else:
            row.value = value
    await db.commit()
    _memo = (now, data)
    log.info("Lista de sets de Scryfall actualizada: %d sets", len(data))
    return data


def icon_name(uri: str | None) -> str | None:
    m = _ICON_URI.match(uri or "")
    return m.group(1) if m else None


def local_icon_url(uri: str | None) -> str | None:
    name = icon_name(uri)
    return f"/api/collection/set-icons/{name}.svg" if name else uri


def _icons_dir() -> Path:
    return cfg.PATHS.thumbs_dir / "set_icons"


async def icon_file(name: str, sets: list[dict[str, Any]]) -> Path | None:
    """El SVG del icono en disco, descargándolo la primera vez. Sólo iconos de sets
    conocidos: la ruta nunca sirve para pedir otra cosa a Scryfall."""
    uri = next((s["icon_svg_uri"] for s in sets if icon_name(s.get("icon_svg_uri")) == name), None)
    if uri is None:
        return None
    path = _icons_dir() / f"{name}.svg"
    if path.exists():
        return path
    async with _icon_locks.setdefault(name, asyncio.Lock()):
        if path.exists():
            return path
        resp = await _http().get(uri)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
    return path


def prefetch_icons(sets: list[dict[str, Any]]) -> None:
    """Descarga en segundo plano los iconos que falten (~370 SVG pequeños), para
    que la colección no los espere uno a uno la primera vez."""
    global _prefetch
    if _prefetch is not None and not _prefetch.done():
        return
    names = {n for s in sets if (n := icon_name(s.get("icon_svg_uri")))}
    missing = [n for n in names if not (_icons_dir() / f"{n}.svg").exists()]
    if not missing:
        return

    async def run() -> None:
        semaphore = asyncio.Semaphore(_PREFETCH_CONCURRENCY)

        async def one(name: str) -> None:
            async with semaphore:
                try:
                    await icon_file(name, sets)
                except httpx.HTTPError as e:
                    log.debug("Icono %s no descargado: %s", name, e)

        await asyncio.gather(*(one(n) for n in missing))
        log.info("Iconos de sets descargados: %d", len(missing))

    _prefetch = asyncio.create_task(run(), name="set-icons-prefetch")
