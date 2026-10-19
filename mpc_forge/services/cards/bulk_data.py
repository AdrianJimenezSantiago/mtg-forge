from __future__ import annotations

import asyncio
import json
import logging
import zlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
from mpc_forge.db import analyze_table, session_scope
from mpc_forge.models import BulkSyncState, PrintingCache
from mpc_forge.services.cards import printings
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

BULK_KINDS = ("default_cards", "oracle_cards")
DEFAULT_KIND = "default_cards"

BATCH_SIZE = 2000

SKIPPED_LAYOUTS = frozenset({"art_series"})

PROGRESS_EVERY = 5000

REFRESH_AFTER = timedelta(days=7)


@dataclass
class BulkProgress:
    kind: str = DEFAULT_KIND
    phase: str = "idle"
    rows_seen: int = 0
    rows_written: int = 0
    bytes_downloaded: int = 0
    bytes_total: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        elapsed = 0.0
        if self.started_at:
            end = self.finished_at or datetime.now(UTC)
            elapsed = (end - self.started_at).total_seconds()
        percent = 0.0
        eta = None
        if self.bytes_total:
            percent = round(100 * self.bytes_downloaded / self.bytes_total, 1)
            remaining = self.bytes_total - self.bytes_downloaded
            if self.phase == "downloading" and self.bytes_downloaded and elapsed > 0:
                eta = round(max(0.0, remaining) * elapsed / self.bytes_downloaded, 1)
        return {
            "kind": self.kind,
            "phase": self.phase,
            "active": self.phase in ("manifest", "downloading", "importing"),
            "rows_seen": self.rows_seen,
            "rows_written": self.rows_written,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "percent": percent,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta,
            "error": self.error,
            "cancelled": self.cancelled,
        }


_progress = BulkProgress()

_task: asyncio.Task[None] | None = None


def get_progress() -> dict[str, Any]:
    return _progress.to_dict()


def is_running() -> bool:
    return _task is not None and not _task.done()


async def cancel() -> bool:
    if _task is None or _task.done():
        return False
    _progress.cancelled = True
    _task.cancel()
    return True


async def fetch_manifest(kind: str = DEFAULT_KIND) -> dict[str, Any]:
    if kind not in BULK_KINDS:
        raise ValueError(f"Volcado desconocido: {kind}. Válidos: {BULK_KINDS}")
    async with httpx.AsyncClient(
        base_url=SCRYFALL_API,
        headers={"User-Agent": SCRYFALL_USER_AGENT, "Accept": "application/json"},
        timeout=60.0,
        verify=not ssl_insecure(),
    ) as client:
        resp = await client.get("/bulk-data")
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            if entry.get("type") == kind:
                return cast(dict[str, Any], entry)
    raise LookupError(f"Scryfall no publica un volcado de tipo {kind!r}")


async def get_state(db: AsyncSession, kind: str = DEFAULT_KIND) -> BulkSyncState | None:
    return await db.get(BulkSyncState, kind)


async def needs_sync(db: AsyncSession, kind: str = DEFAULT_KIND) -> tuple[bool, str]:
    state = await get_state(db, kind)
    try:
        manifest = await fetch_manifest(kind)
    except Exception as e:
        return (False, f"No se pudo consultar el catálogo de Scryfall: {e}")
    remote = manifest.get("updated_at", "")
    if state is None or not state.updated_at:
        return (True, "Nunca se ha importado el volcado")
    if remote > state.updated_at:
        return (True, f"Hay un volcado más reciente ({remote[:10]})")
    return (False, f"Ya está al día ({state.updated_at[:10]})")


async def _jsonl_cards(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any]]:
    """Descomprime al vuelo un .jsonl.gz y emite una carta por línea."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pending = b""

    def lines(data: bytes) -> Iterator[dict[str, Any]]:
        nonlocal pending
        *complete, pending = (pending + data).split(b"\n")
        for line in complete:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("Línea ilegible en el volcado; se omite")

    async for chunk in chunks:
        while chunk:
            for card in lines(inflater.decompress(chunk)):
                yield card
            # Un .gz puede traer varios miembros concatenados.
            chunk = inflater.unused_data if inflater.eof else b""
            if inflater.eof:
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for card in lines(inflater.flush() + b"\n"):
        yield card


def card_to_row(card: dict[str, Any]) -> dict[str, Any] | None:
    scryfall_id = card.get("id")
    if not scryfall_id:
        return None
    if (card.get("layout") or "normal") in SKIPPED_LAYOUTS:
        return None
    return {"scryfall_id": scryfall_id, **printings.printing_fields(card)}


async def _flush(db: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    stmt = sqlite_insert(PrintingCache)
    update_cols = {
        c.name: getattr(stmt.excluded, c.name)
        for c in PrintingCache.__table__.columns
        if c.name != "scryfall_id"
    }
    # executemany: una sentencia por fila. Con .values(rows) serían ~80k
    # parámetros por lote, por encima del límite de SQLite en Windows (32766).
    await db.execute(
        stmt.on_conflict_do_update(index_elements=["scryfall_id"], set_=update_cols), rows
    )
    await db.commit()
    return len(rows)


async def sync(
    db: AsyncSession,
    kind: str = DEFAULT_KIND,
    *,
    force: bool = False,
) -> dict[str, Any]:
    global _progress
    _progress = BulkProgress(kind=kind, phase="manifest", started_at=datetime.now(UTC))

    try:
        manifest = await fetch_manifest(kind)
        remote_updated = manifest.get("updated_at", "")
        _progress.bytes_total = int(manifest.get("compressed_size", 0) or 0)

        state = await get_state(db, kind)
        if not force and state and state.updated_at == remote_updated:
            _progress.phase = "done"
            _progress.finished_at = datetime.now(UTC)
            return {
                "skipped": True,
                "reason": "El volcado local ya está al día",
                "updated_at": remote_updated,
            }

        url = manifest.get("jsonl_download_uri")
        if not url:
            raise LookupError(f"El catálogo de Scryfall no trae URL de descarga para {kind!r}")
        log.info(
            "Importando volcado %s (%s, %.0f MB)",
            kind,
            remote_updated[:10],
            _progress.bytes_total / 1e6,
        )

        _progress.phase = "downloading"
        written = await _stream_import(db, url, kind)

        _progress.phase = "importing"
        await _record_state(db, kind, remote_updated, written, _progress.bytes_downloaded)

        _progress.phase = "done"
        _progress.finished_at = datetime.now(UTC)
        log.info("Volcado %s importado: %d impresiones", kind, written)
        return {
            "skipped": False,
            "kind": kind,
            "updated_at": remote_updated,
            "rows_imported": written,
        }

    except asyncio.CancelledError:
        _progress.phase = "error"
        _progress.cancelled = True
        _progress.error = "Cancelado por el usuario"
        _progress.finished_at = datetime.now(UTC)
        raise
    except Exception as e:
        _progress.phase = "error"
        _progress.error = str(e)
        _progress.finished_at = datetime.now(UTC)
        log.exception("La importación del volcado %s falló", kind)
        raise


async def _stream_import(db: AsyncSession, url: str, kind: str) -> int:
    batch: list[dict[str, Any]] = []
    written = 0
    seen_ids: set[str] = set()

    async with (
        httpx.AsyncClient(
            headers={"User-Agent": SCRYFALL_USER_AGENT},
            timeout=httpx.Timeout(60.0, read=300.0),
            follow_redirects=True,
            verify=not ssl_insecure(),
        ) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()

        async def raw_chunks() -> AsyncIterator[bytes]:
            # aiter_raw: el .gz tal cual, lo sirva o no el CDN con Content-Encoding.
            async for chunk in response.aiter_raw(chunk_size=1 << 20):
                _progress.bytes_downloaded += len(chunk)
                yield chunk

        async for card in _jsonl_cards(raw_chunks()):
            _progress.rows_seen += 1
            row = card_to_row(card)
            if row is None or row["scryfall_id"] in seen_ids:
                continue
            seen_ids.add(row["scryfall_id"])
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                written += await _flush(db, batch)
                batch.clear()
                seen_ids.clear()
                _progress.rows_written = written
                await asyncio.sleep(0)

    written += await _flush(db, batch)
    _progress.rows_written = written
    return written


async def _record_state(db: AsyncSession, kind: str, updated_at: str, rows: int, size: int) -> None:
    state = await db.get(BulkSyncState, kind)
    if state is None:
        state = BulkSyncState(kind=kind)
        db.add(state)
    state.updated_at = updated_at
    state.synced_at = datetime.now(UTC)
    state.rows_imported = rows
    state.bytes_downloaded = size
    await db.commit()


def start(kind: str = DEFAULT_KIND, *, force: bool = False) -> asyncio.Task[None]:
    global _task
    if is_running():
        raise RuntimeError("Ya hay una importación del volcado en curso")

    async def _run() -> None:
        async with session_scope() as session:
            await sync(session, kind, force=force)
        await analyze_table("printings")

    def _done(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    _task = asyncio.create_task(_run(), name=f"bulk-sync-{kind}")
    _task.add_done_callback(_done)
    return _task


async def refresh_if_stale(db: AsyncSession, kind: str = DEFAULT_KIND) -> bool:
    """Reimporta en segundo plano un volcado ya importado si tiene más de REFRESH_AFTER.

    Nunca hace la primera importación (son cientos de MB): eso lo decide el usuario.
    """
    state = await get_state(db, kind)
    if state is None or state.synced_at is None or is_running():
        return False
    synced_at = state.synced_at if state.synced_at.tzinfo else state.synced_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - synced_at < REFRESH_AFTER:
        return False
    needed, reason = await needs_sync(db, kind)
    if not needed:
        log.info("Volcado %s: %s", kind, reason)
        return False
    log.info("Volcado %s desactualizado: %s. Reimportando en segundo plano.", kind, reason)
    start(kind)
    return True


async def local_stats(db: AsyncSession) -> dict[str, Any]:
    total = (await db.scalar(select(func.count()).select_from(PrintingCache))) or 0
    unique = (await db.scalar(select(func.count(func.distinct(PrintingCache.oracle_id))))) or 0
    states = (await db.execute(select(BulkSyncState))).scalars().all()
    return {
        "printings": total,
        "unique_cards": unique,
        "syncs": [
            {
                "kind": s.kind,
                "updated_at": s.updated_at,
                "synced_at": s.synced_at.isoformat() if s.synced_at else None,
                "rows_imported": s.rows_imported,
                "bytes_downloaded": s.bytes_downloaded,
            }
            for s in states
        ],
    }
