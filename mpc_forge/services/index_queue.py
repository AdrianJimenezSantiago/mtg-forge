"""Cola centralizada de indexado de art sources.

Resuelve el problema de lanzar N background tasks independientes (una por
drive) que compiten por el lock de SQLite y la cuota de Google Drive API.

Arquitectura:
  - Una ``asyncio.Queue`` con un único worker que procesa drives uno a uno.
  - Estado in-memory (como ``build_progress.py``) consultable vía polling.
  - Cancelación cooperativa: el worker comprueba un flag entre drives.

Flujo:
  1. Frontend llama ``POST /api/drives/index-batch`` con una lista de IDs.
  2. El endpoint llama ``index_queue.enqueue(ids)``.
  3. ``enqueue()`` arranca el worker si no está corriendo.
  4. El worker procesa secuencialmente, actualizando ``IndexJob`` en cada paso.
  5. Frontend hace polling a ``GET /api/drives/index-progress`` cada 2-3s.
  6. Opcionalmente ``POST /api/drives/index-cancel`` marca el flag de stop.

La app corre en single-process, así que un dict local basta — no hace falta
Redis. Si en el futuro se despliega con múltiples workers, este módulo
necesitaría cambiar a un store compartido.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modelo de estado por drive
# ---------------------------------------------------------------------------

class JobStatus(StrEnum):
    QUEUED = "queued"
    INDEXING = "indexing"
    DONE = "done"
    ERROR = "error"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class IndexJob:
    """Estado de indexado de un solo source dentro de un batch."""
    source_id: int
    source_name: str
    status: JobStatus = JobStatus.QUEUED
    files_added: int = 0
    files_updated: int = 0
    files_total: int = 0          # indexed_files reportados por partial commits
    folders_visited: int = 0
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "status": self.status.value,
            "files_added": self.files_added,
            "files_updated": self.files_updated,
            "files_total": self.files_total,
            "folders_visited": self.folders_visited,
            "error": self.error,
            "elapsed_seconds": round(self.elapsed, 1),
        }


# ---------------------------------------------------------------------------
# Cola + worker
# ---------------------------------------------------------------------------

class IndexQueue:
    """Cola de indexado con un único worker asyncio.

    Thread-safe dentro del event loop (todo es async, no hay threads).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._jobs: dict[int, IndexJob] = {}       # source_id → job
        self._order: list[int] = []                 # orden de encolado (para UI)
        self._worker_task: asyncio.Task | None = None
        self._cancel_flag: bool = False
        self._batch_id: str | None = None
        self._batch_started_at: float | None = None

    # -- API pública --

    async def enqueue(
        self,
        source_ids: list[int],
        names: dict[int, str] | None = None,
        *,
        skip_already_indexed: bool = False,
    ) -> dict[str, Any]:
        """Encola una lista de source_ids para indexar.

        Args:
            source_ids: IDs de ArtSource a indexar.
            names: dict {id: name} para mostrar en la UI. Si no se pasa,
                   se usan nombres genéricos.
            skip_already_indexed: si True, los sources con indexed_at no-null
                se marcan como SKIPPED en vez de reindexarse.

        Returns:
            dict con {batch_id, queued, skipped}.
        """
        names = names or {}
        self._cancel_flag = False
        self._batch_id = uuid.uuid4().hex[:12]
        self._batch_started_at = time.time()

        # Limpiar jobs anteriores completados
        self._jobs = {
            k: v for k, v in self._jobs.items()
            if v.status in (JobStatus.QUEUED, JobStatus.INDEXING)
        }
        self._order = [k for k in self._order if k in self._jobs]

        queued = 0
        skipped = 0

        for sid in source_ids:
            # No encolar duplicados
            if sid in self._jobs and self._jobs[sid].status in (
                JobStatus.QUEUED, JobStatus.INDEXING,
            ):
                continue

            name = names.get(sid, f"Source #{sid}")
            self._jobs[sid] = IndexJob(source_id=sid, source_name=name)
            self._order.append(sid)
            await self._queue.put(sid)
            queued += 1

        # Arrancar worker si no está corriendo
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(
                self._worker(), name="index-queue-worker",
            )

        log.info(
            "IndexQueue: batch %s encolado — %d drives (%d skipped)",
            self._batch_id, queued, skipped,
        )
        return {
            "batch_id": self._batch_id,
            "queued": queued,
            "skipped": skipped,
        }

    def cancel(self) -> dict[str, Any]:
        """Marca la cancelación. El worker la comprueba entre drives."""
        self._cancel_flag = True
        cancelled_count = 0
        for job in self._jobs.values():
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                cancelled_count += 1
        log.info("IndexQueue: cancelación solicitada (%d pendientes cancelados)",
                 cancelled_count)
        return {"cancelled": cancelled_count}

    def progress(self) -> dict[str, Any]:
        """Snapshot del estado completo para el frontend."""
        active: dict[str, Any] | None = None
        queued: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []

        for sid in self._order:
            job = self._jobs.get(sid)
            if job is None:
                continue
            d = job.to_dict()
            if job.status == JobStatus.INDEXING:
                active = d
            elif job.status == JobStatus.QUEUED:
                queued.append(d)
            else:
                # DONE, ERROR, SKIPPED, CANCELLED
                completed.append(d)

        total_jobs = len(self._order)
        done_count = len(completed)
        all_done = active is None and len(queued) == 0 and total_jobs > 0

        # ETA: media de elapsed de los completados exitosamente
        done_times = [
            j.elapsed for j in self._jobs.values()
            if j.status == JobStatus.DONE and j.elapsed > 0
        ]
        avg_time = (sum(done_times) / len(done_times)) if done_times else None
        eta_seconds: float | None = None
        remaining = len(queued) + (1 if active else 0)
        if avg_time and remaining > 0:
            # Para el activo, descontamos lo que ya lleva
            active_remaining = 0.0
            if active:
                active_job = next(
                    (j for j in self._jobs.values() if j.status == JobStatus.INDEXING),
                    None,
                )
                if active_job and avg_time > active_job.elapsed:
                    active_remaining = avg_time - active_job.elapsed
            eta_seconds = active_remaining + avg_time * len(queued)

        batch_elapsed = 0.0
        if self._batch_started_at:
            batch_elapsed = time.time() - self._batch_started_at

        return {
            "batch_id": self._batch_id,
            "active": active,
            "queued": queued,
            "completed": completed,
            "total": total_jobs,
            "done_count": done_count,
            "all_done": all_done,
            "cancelled": self._cancel_flag,
            "eta_seconds": round(eta_seconds, 1) if eta_seconds is not None else None,
            "batch_elapsed_seconds": round(batch_elapsed, 1),
            "percent": round(100 * done_count / total_jobs, 1) if total_jobs > 0 else 0,
        }

    def is_running(self) -> bool:
        """True si hay un worker activo procesando la cola."""
        return (
            self._worker_task is not None
            and not self._worker_task.done()
        )

    def clear_completed(self) -> None:
        """Limpia el historial de jobs completados."""
        self._jobs = {
            k: v for k, v in self._jobs.items()
            if v.status in (JobStatus.QUEUED, JobStatus.INDEXING)
        }
        self._order = [k for k in self._order if k in self._jobs]
        if not self._order:
            self._batch_id = None
            self._batch_started_at = None

    # -- Worker interno --

    async def _worker(self) -> None:
        """Procesa la cola secuencialmente (un drive a la vez).

        Usa su propia sesión de BD por drive para aislar transacciones.
        Nunca lanza excepciones — captura todo y marca el job con ERROR.
        """
        from mpc_forge.db import session_scope
        from mpc_forge.services import gdrive_indexer

        log.info("IndexQueue worker arrancado")

        while not self._queue.empty():
            sid = await self._queue.get()
            job = self._jobs.get(sid)
            if job is None:
                continue

            # ¿Cancelación?
            if self._cancel_flag or job.status == JobStatus.CANCELLED:
                if job.status != JobStatus.CANCELLED:
                    job.status = JobStatus.CANCELLED
                # Drenar el resto de la cola
                while not self._queue.empty():
                    remaining_sid = self._queue.get_nowait()
                    rj = self._jobs.get(remaining_sid)
                    if rj and rj.status == JobStatus.QUEUED:
                        rj.status = JobStatus.CANCELLED
                log.info("IndexQueue worker: cancelación, drenada la cola")
                break

            # Indexar este drive
            job.status = JobStatus.INDEXING
            job.started_at = time.time()
            log.info("IndexQueue: indexando source %d (%s)", sid, job.source_name)

            try:
                async with session_scope() as db:
                    result = await gdrive_indexer.index_source(
                        db, sid,
                        on_progress=self._make_progress_callback(sid),
                    )

                job.files_added = result.files_added
                job.files_updated = result.files_updated
                job.folders_visited = result.folders_visited
                job.error = result.error
                job.status = JobStatus.ERROR if result.error else JobStatus.DONE

                # Actualizar files_total con el count real post-indexado
                if not result.error:
                    try:
                        async with session_scope() as db:
                            from mpc_forge.models import ArtSource
                            src = await db.get(ArtSource, sid)
                            if src:
                                job.files_total = src.indexed_files or 0
                    except Exception:
                        pass

            except Exception as e:
                log.exception("IndexQueue: fallo indexando source %d", sid)
                job.status = JobStatus.ERROR
                job.error = f"{type(e).__name__}: {str(e)[:200]}"

                # Intentar marcar el error en BD
                try:
                    async with session_scope() as db:
                        from datetime import datetime

                        from mpc_forge.models import ArtSource
                        src = await db.get(ArtSource, sid)
                        if src:
                            src.indexed_at = datetime.now(UTC)
                            src.index_error = job.error or ""
                except Exception:
                    log.exception("IndexQueue: no se pudo marcar error en BD para source %d", sid)

            finally:
                job.finished_at = time.time()
                log.info(
                    "IndexQueue: source %d (%s) terminado en %.1fs — status=%s, "
                    "+%d/~%d archivos, error=%s",
                    sid, job.source_name, job.elapsed,
                    job.status.value, job.files_added, job.files_updated,
                    job.error or "ninguno",
                )

        log.info("IndexQueue worker terminado")

    def _make_progress_callback(self, source_id: int):
        """Devuelve un callable que ``index_source`` invoca periódicamente
        para reportar progreso parcial.

        Signatura del callback: ``(files_added, files_updated, folders_visited) -> None``
        """
        def callback(
            files_added: int = 0,
            files_updated: int = 0,
            folders_visited: int = 0,
            files_total: int = 0,
        ) -> None:
            job = self._jobs.get(source_id)
            if job is None:
                return
            job.files_added = files_added
            job.files_updated = files_updated
            job.folders_visited = folders_visited
            if files_total:
                job.files_total = files_total
        return callback


# ---------------------------------------------------------------------------
# Singleton global
# ---------------------------------------------------------------------------

_queue = IndexQueue()


def get_queue() -> IndexQueue:
    """Devuelve la instancia singleton de la cola de indexado."""
    return _queue
