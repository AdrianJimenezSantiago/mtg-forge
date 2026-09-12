"""Importación del bulk data de Scryfall — el modo offline completo.

El problema
-----------
Cada carta que la app no conoce es una petición HTTP a Scryfall, con un rate
limit autoimpuesto de ~100 ms entre llamadas. Consecuencias medidas:

* importar un Commander de 100 cartas en frío: 10-30 s
* precargar las impresiones alternativas de ese mazo: minutos
* abrir el selector de arte de una carta no precargada: 5-15 s
* sin internet: nada de lo anterior funciona

La solución
-----------
Scryfall publica volcados completos de su base de datos en
``/bulk-data``. El volcado ``default_cards`` contiene TODAS las impresiones de
TODAS las cartas (~500 MB de JSON, ~120 MB comprimido). Importándolo una vez a
``PrintingCache``, todas esas operaciones pasan a ser consultas locales.

Por qué es opt-in
-----------------
Ocupa cerca de 1 GB en SQLite y exige descargar más de 100 MB. Un usuario que
solo quiere imprimir un mazo no debería pagar eso. Se activa desde Ajustes y
se puede revertir.

Streaming, no ``json.load``
---------------------------
El volcado no cabe cómodamente en memoria: cargarlo entero son varios GB de
objetos Python. Se procesa como flujo con ``ijson`` si está disponible, y con
un parser incremental propio si no. Nunca hay más de un lote en memoria.

Reanudable e idempotente
------------------------
Se registra el ``updated_at`` del manifiesto en ``BulkSyncState``. Si no ha
cambiado desde la última importación, no se descarga nada. Los ``INSERT`` son
``ON CONFLICT DO UPDATE`` sobre la clave primaria, así que reimportar no
duplica: refresca.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
from mpc_forge.models import BulkSyncState, PrintingCache
from mpc_forge.services.deck_service import _normalize_card_name  # noqa: F401
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

# Volcados que tiene sentido importar.
#   default_cards → una fila por impresión en inglés (lo que necesita el
#                   selector de arte). Es el que queremos.
#   oracle_cards  → una fila por carta única. Mucho más pequeño, pero no sirve
#                   para el selector porque no trae las impresiones.
BULK_KINDS = ("default_cards", "oracle_cards")
DEFAULT_KIND = "default_cards"

# Filas por transacción. 2.000 mantiene el uso de memoria bajo y evita que una
# sola transacción bloquee la BD durante segundos con WAL.
BATCH_SIZE = 2000

# Cada cuántas filas se refresca el progreso que consulta la interfaz.
PROGRESS_EVERY = 5000


@dataclass
class BulkProgress:
    """Estado observable de una importación en curso.

    Vive en memoria: si la app se cierra a media importación, la siguiente
    empieza de cero. Es aceptable porque la operación es idempotente y el
    cuello de botella real es la descarga, no el parseo.
    """
    kind: str = DEFAULT_KIND
    phase: str = "idle"          # idle|manifest|downloading|importing|done|error
    rows_seen: int = 0
    rows_written: int = 0
    bytes_downloaded: int = 0
    bytes_total: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled: bool = False
    _task: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        elapsed = 0.0
        if self.started_at:
            end = self.finished_at or datetime.now(UTC)
            elapsed = (end - self.started_at).total_seconds()
        percent = 0.0
        if self.bytes_total:
            percent = round(100 * self.bytes_downloaded / self.bytes_total, 1)
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
            "error": self.error,
            "cancelled": self.cancelled,
        }


# Una importación a la vez. Descargar dos volcados en paralelo saturaría la
# red y multiplicaría la contención de escritura en SQLite sin ganar nada.
_progress = BulkProgress()


def get_progress() -> dict[str, Any]:
    return _progress.to_dict()


async def cancel() -> bool:
    """Cancela la importación en curso. Lo ya importado se conserva."""
    if _progress._task is None or _progress._task.done():
        return False
    _progress.cancelled = True
    _progress._task.cancel()
    return True


# ---------------------------------------------------------------------------
# Manifiesto
# ---------------------------------------------------------------------------

async def fetch_manifest(kind: str = DEFAULT_KIND) -> dict[str, Any]:
    """Devuelve la entrada del catálogo de bulk data para ``kind``."""
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
                return entry
    raise LookupError(f"Scryfall no publica un volcado de tipo {kind!r}")


async def get_state(db: AsyncSession, kind: str = DEFAULT_KIND) -> BulkSyncState | None:
    return await db.get(BulkSyncState, kind)


async def needs_sync(db: AsyncSession, kind: str = DEFAULT_KIND) -> tuple[bool, str]:
    """¿Hay una versión más nueva que la importada?

    Devuelve ``(hace_falta, motivo)``. El motivo se muestra en Ajustes para que
    el usuario entienda por qué se le ofrece (o no) actualizar.
    """
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


# ---------------------------------------------------------------------------
# Parseo en streaming
# ---------------------------------------------------------------------------

def _ijson_available() -> bool:
    try:
        import ijson  # noqa: F401
        return True
    except ImportError:
        return False


class _IncrementalArrayParser:
    """Extrae objetos de un array JSON de nivel superior, trozo a trozo.

    Es el respaldo para cuando ``ijson`` no está instalado. El volcado de
    Scryfall tiene exactamente la forma ``[{...},{...},...]``, así que basta
    con contar llaves fuera de cadenas y emitir cada objeto completo.

    No pretende ser un parser JSON general — solo tiene que sobrevivir a esta
    forma concreta sin cargar los 500 MB del fichero en memoria.

    Detalle crítico: se mantiene ``_pos``, la posición hasta la que ya se ha
    escaneado. Sin ella, cada llamada a ``feed`` volvería a recorrer el buffer
    desde el principio y recontaría llaves que ya se habían contado, con lo que
    la profundidad nunca volvería a cero y no se emitiría ni un solo objeto.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._pos = 0          # hasta dónde se ha escaneado ya
        self._depth = 0
        self._start = -1       # inicio del objeto en curso, o -1
        self._in_string = False
        self._escaped = False

    def feed(self, chunk: str) -> Iterator[dict[str, Any]]:
        self._buf += chunk
        i = self._pos
        buf = self._buf

        while i < len(buf):
            ch = buf[i]
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
            elif ch == '"':
                self._in_string = True
            elif ch == "{":
                if self._depth == 0:
                    self._start = i
                self._depth += 1
            elif ch == "}":
                self._depth -= 1
                if self._depth == 0 and self._start >= 0:
                    raw = buf[self._start:i + 1]
                    self._start = -1
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("Objeto ilegible en el volcado; se omite")
                    # Se descarta todo lo consumido: el buffer nunca crece más
                    # allá del objeto en curso más el último trozo recibido.
                    buf = buf[i + 1:]
                    self._buf = buf
                    i = 0
                    continue
            i += 1

        # Si estamos a mitad de un objeto, se recorta lo anterior a su inicio
        # y se reajustan los índices para no volver a escanearlo.
        if self._depth > 0 and self._start > 0:
            self._buf = buf[self._start:]
            i -= self._start
            self._start = 0
        elif self._depth == 0 and self._start < 0:
            # Entre objetos solo quedan comas y espacios: no vale la pena
            # conservarlos.
            self._buf = ""
            i = 0

        self._pos = i


# ---------------------------------------------------------------------------
# Mapeo a PrintingCache
# ---------------------------------------------------------------------------

def card_to_row(card: dict[str, Any]) -> dict[str, Any] | None:
    """Convierte un objeto de Scryfall en una fila de ``PrintingCache``.

    Devuelve ``None`` para las cartas que no queremos indexar: sin ``oracle_id``
    (tokens de arte, cartas de doble cara mal formadas) o de tipos que nunca se
    imprimen como proxy.
    """
    scryfall_id = card.get("id")
    oracle_id = card.get("oracle_id")
    if not scryfall_id:
        return None

    layout = card.get("layout") or "normal"
    # `art_series` y `double_faced_token` no son cartas jugables; ocupan sitio
    # y ensucian el selector.
    if layout in ("art_series", "double_faced_token"):
        return None

    faces = card.get("card_faces") or []
    front_img = card.get("image_uris") or {}
    back_img: dict[str, str] = {}
    back_name = None
    mana_cost = card.get("mana_cost") or ""
    type_line = card.get("type_line") or ""
    colors = card.get("colors") or []

    if faces:
        front_img = faces[0].get("image_uris", front_img) or front_img
        mana_cost = faces[0].get("mana_cost", mana_cost) or mana_cost
        type_line = faces[0].get("type_line", type_line) or type_line
        colors = faces[0].get("colors", colors) or colors
        if len(faces) > 1:
            back_img = faces[1].get("image_uris", {}) or {}
            back_name = faces[1].get("name")

    related = [
        {
            "id": part.get("id", ""),
            "name": part.get("name", ""),
            "component": part.get("component", ""),
        }
        for part in (card.get("all_parts") or [])
        if part.get("component") in ("token", "meld_result", "meld_part")
    ]

    return {
        "scryfall_id": scryfall_id,
        "oracle_id": oracle_id or "",
        "name": card.get("name", ""),
        "set_code": (card.get("set") or "").lower(),
        "set_name": card.get("set_name") or "",
        "collector_number": card.get("collector_number") or "",
        "rarity": card.get("rarity") or "",
        "lang": card.get("lang") or "en",
        "frame": card.get("frame") or "",
        "border_color": card.get("border_color") or "",
        "full_art": bool(card.get("full_art", False)),
        "textless": bool(card.get("textless", False)),
        "promo": bool(card.get("promo", False)),
        "layout": layout,
        "mana_cost": mana_cost,
        "cmc": float(card.get("cmc", 0.0) or 0.0),
        "type_line": type_line,
        "colors": ",".join(colors),
        "color_identity": ",".join(card.get("color_identity") or []),
        "keywords": ",".join(card.get("keywords") or []),
        "image_normal": front_img.get("normal"),
        "image_large": front_img.get("large"),
        "image_png": front_img.get("png"),
        "back_image_normal": back_img.get("normal") or None,
        "back_image_large": back_img.get("large") or None,
        "back_image_png": back_img.get("png") or None,
        "back_name": back_name,
        "artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "finishes": ",".join(card.get("finishes") or []),
        "related_parts": json.dumps(related, ensure_ascii=False) if related else "",
        "fetched_at": datetime.now(UTC),
    }


async def _flush(db: AsyncSession, rows: list[dict[str, Any]]) -> int:
    """Escribe un lote con upsert sobre la clave primaria.

    ``ON CONFLICT DO UPDATE`` en vez de borrar y reinsertar: así una
    reimportación refresca los datos sin invalidar ni un instante las
    referencias de ``deck_cards.scryfall_id``.
    """
    if not rows:
        return 0
    stmt = sqlite_insert(PrintingCache).values(rows)
    update_cols = {
        c.name: getattr(stmt.excluded, c.name)
        for c in PrintingCache.__table__.columns
        if c.name != "scryfall_id"
    }
    await db.execute(
        stmt.on_conflict_do_update(index_elements=["scryfall_id"], set_=update_cols)
    )
    await db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Importación
# ---------------------------------------------------------------------------

async def sync(
    db: AsyncSession,
    kind: str = DEFAULT_KIND,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Descarga e importa el volcado. Bloquea hasta terminar.

    Para dispararlo en segundo plano usa :func:`start`.
    """
    global _progress
    _progress = BulkProgress(kind=kind, phase="manifest",
                             started_at=datetime.now(UTC))

    try:
        manifest = await fetch_manifest(kind)
        remote_updated = manifest.get("updated_at", "")
        _progress.bytes_total = int(manifest.get("size", 0) or 0)

        state = await get_state(db, kind)
        if not force and state and state.updated_at == remote_updated:
            _progress.phase = "done"
            _progress.finished_at = datetime.now(UTC)
            return {
                "skipped": True,
                "reason": "El volcado local ya está al día",
                "updated_at": remote_updated,
            }

        url = manifest["download_uri"]
        log.info("Importando volcado %s (%s, %.0f MB)",
                 kind, remote_updated[:10], _progress.bytes_total / 1e6)

        _progress.phase = "downloading"
        written = await _stream_import(db, url, kind)

        _progress.phase = "importing"
        await _record_state(db, kind, remote_updated, written,
                            _progress.bytes_downloaded)

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
    """Descarga y escribe por lotes. Devuelve cuántas filas se escribieron."""
    use_ijson = _ijson_available()
    parser = None if use_ijson else _IncrementalArrayParser()
    batch: list[dict[str, Any]] = []
    written = 0
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(
        headers={"User-Agent": SCRYFALL_USER_AGENT},
        timeout=httpx.Timeout(60.0, read=300.0),
        follow_redirects=True,
        verify=not ssl_insecure(),
    ) as client, client.stream("GET", url) as response:
        response.raise_for_status()

        async def handle(card: dict[str, Any]) -> None:
            nonlocal written
            _progress.rows_seen += 1
            row = card_to_row(card)
            if row is None:
                return
            # El volcado no repite ids, pero un fallo de red que provoque
            # un reintento parcial sí podría. Insertar dos veces la misma
            # PK en un solo lote rompe el upsert de SQLite.
            if row["scryfall_id"] in seen_ids:
                return
            seen_ids.add(row["scryfall_id"])
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                written += await _flush(db, batch)
                batch.clear()
                seen_ids.clear()
                _progress.rows_written = written
                # Cede el control: sin esto el event loop se queda
                # bloqueado y la interfaz deja de responder al polling
                # de progreso durante toda la importación.
                await asyncio.sleep(0)

        if use_ijson:
            import ijson

            async def byte_chunks():
                async for chunk in response.aiter_bytes(chunk_size=1 << 20):
                    _progress.bytes_downloaded += len(chunk)
                    yield chunk

            async for card in ijson.items_async(byte_chunks(), "item"):
                await handle(card)
        else:
            async for chunk in response.aiter_text(chunk_size=1 << 20):
                _progress.bytes_downloaded += len(chunk.encode("utf-8"))
                for card in parser.feed(chunk):
                    await handle(card)

    written += await _flush(db, batch)
    _progress.rows_written = written
    return written


async def _record_state(
    db: AsyncSession, kind: str, updated_at: str, rows: int, size: int
) -> None:
    state = await db.get(BulkSyncState, kind)
    if state is None:
        state = BulkSyncState(kind=kind)
        db.add(state)
    state.updated_at = updated_at
    state.synced_at = datetime.now(UTC)
    state.rows_imported = rows
    state.bytes_downloaded = size
    await db.commit()


def start(db_factory, kind: str = DEFAULT_KIND, *, force: bool = False):
    """Lanza la importación en segundo plano y devuelve la tarea.

    ``db_factory`` es un context manager async que produce una sesión — se
    recibe en vez de una sesión abierta porque la importación dura minutos y
    mantener abierta la sesión del request sería un error.
    """
    async def _run() -> None:
        async with db_factory() as session:
            await sync(session, kind, force=force)

    task = asyncio.create_task(_run(), name=f"bulk-sync-{kind}")
    _progress._task = task
    return task


async def local_stats(db: AsyncSession) -> dict[str, Any]:
    """Cuántas impresiones hay en local y de cuándo es el volcado."""
    total = (await db.scalar(select(func.count()).select_from(PrintingCache))) or 0
    unique = (await db.scalar(
        select(func.count(func.distinct(PrintingCache.oracle_id)))
    )) or 0
    states = (await db.execute(select(BulkSyncState))).scalars().all()
    return {
        "printings": total,
        "unique_cards": unique,
        "ijson_available": _ijson_available(),
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
