from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "La tarea de background %r terminó con excepción",
                task.get_name(),
                exc_info=exc,
            )

    async def shutdown(self, timeout: float = 5.0) -> None:
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        log.info("Cancelando %d tareas de background…", len(pending))
        for task in pending:
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)
        except TimeoutError:
            log.warning(
                "%d tareas no respondieron a la cancelación en %.0fs; se continúa con el cierre.",
                len(pending),
                timeout,
            )
