"""Precarga en background de impresiones alternativas para las cartas de un mazo.

Cuando el usuario abre un mazo, descargamos en background todas las
impresiones de sus cartas. Así cuando abre el modal de arte para cualquier
carta ya está cacheado y la respuesta es instantánea desde BD (sin llamar a
Scryfall).

Estado por ``deck_id``, cancelable, y expuesto vía ``to_dict()`` para que el
frontend haga polling y muestre progreso. La app corre en single-process, así
que un dict en memoria basta — no hace falta persistir en BD.

Concurrencia
------------
Cada impresión alternativa sale de ``/cards/search``, que Scryfall limita a
2 peticiones/s. El semáforo es **global** (compartido por todos los mazos): si
el usuario importa y abre cinco mazos seguidos, las cinco precargas comparten
tres huecos en vez de disparar 4 × 5 peticiones a la vez. Más huecos no darían
más velocidad (el límite lo pone Scryfall), solo una cola más larga.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import session_scope
from mpc_forge.models import DeckCard
from mpc_forge.services import deck_service

log = logging.getLogger(__name__)


@dataclass
class PreloadState:
    """Snapshot del progreso de la precarga de un mazo."""
    deck_id: int
    total: int = 0
    done: int = 0
    in_progress: bool = False
    # El task no se serializa; solo lo usamos internamente para cancelar.
    task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deck_id": self.deck_id,
            "total": self.total,
            "done": self.done,
            "in_progress": self.in_progress,
        }


# Registro global: deck_id → estado. Vive lo que viva el proceso.
_states: dict[int, PreloadState] = {}


async def start(deck_id: int, scryfall: ScryfallClient) -> PreloadState:
    """Arranca precarga en background. Cancela la anterior del mismo mazo."""
    prev = _states.get(deck_id)
    if prev and prev.task and not prev.task.done():
        prev.task.cancel()
        try:
            await prev.task
        except (asyncio.CancelledError, Exception):
            pass

    state = PreloadState(deck_id=deck_id, in_progress=True)
    _states[deck_id] = state
    state.task = asyncio.create_task(_run(state, scryfall))
    return state


def get_state(deck_id: int) -> PreloadState | None:
    return _states.get(deck_id)


async def cancel(deck_id: int) -> None:
    state = _states.get(deck_id)
    if state and state.task and not state.task.done():
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):
            pass
    if state:
        state.in_progress = False


_PRELOAD_CONCURRENCY = 3

# Semáforo compartido por todas las precargas. Se crea perezosamente y ligado
# al event loop en curso (asyncio.Semaphore no puede cruzar loops, algo que sí
# ocurre entre tests).
_semaphore: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _shared_semaphore() -> asyncio.Semaphore:
    global _semaphore
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore[0] is not loop:
        _semaphore = (loop, asyncio.Semaphore(_PRELOAD_CONCURRENCY))
    return _semaphore[1]


async def _run(state: PreloadState, scryfall: ScryfallClient) -> None:
    """Trae las impresiones alternativas de las cartas del mazo.

    1. Una consulta local descarta las cartas cuyas impresiones ya se tienen.
    2. Las restantes se piden en lotes de ``PRINTS_BATCH_SIZE`` oracle_ids por
       búsqueda (ver :meth:`ScryfallClient.prints_by_oracle_ids`).
    3. La sesión de BD se abre solo para guardar, nunca mientras se espera a
       la red: con varias precargas en cola no se acaparan conexiones.

    Si una búsqueda por lotes falla con un error del cliente (4xx), ese lote
    se reintenta carta a carta para no perder las demás.
    """
    try:
        async with session_scope() as db:
            oracle_ids: list[str] = list({
                r for r in (
                    await db.scalars(
                        select(DeckCard.oracle_id).where(
                            DeckCard.deck_id == state.deck_id,
                            DeckCard.oracle_id != "",
                        )
                    )
                ).all() if r
            })
            pending = await deck_service.pending_print_oracles(db, oracle_ids)
        state.total = len(oracle_ids)
        state.done = len(oracle_ids) - len(pending)
        if not pending:
            return

        semaphore = _shared_semaphore()
        size = deck_service.PRINTS_BATCH_SIZE
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]

        async def _fetch(batch: list[str]) -> None:
            try:
                prints = await scryfall.prints_by_oracle_ids(batch)
            except httpx.HTTPStatusError as e:
                if not (400 <= e.response.status_code < 500) or e.response.status_code == 429:
                    raise
                prints = []
                for oid in batch:
                    prints.extend(await scryfall.prints_by_oracle_id(oid))
            async with session_scope() as db:
                await deck_service.store_prints(db, batch, prints)

        async def _one(batch: list[str]) -> None:
            async with semaphore:
                try:
                    await _fetch(batch)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("Preload de %d oracles falló: %s", len(batch), e)
                finally:
                    state.done += len(batch)

        await asyncio.gather(*(_one(b) for b in batches))
    except asyncio.CancelledError:
        log.debug("Preload del mazo %s cancelado", state.deck_id)
    except Exception as e:
        log.warning("Preload del mazo %s falló: %s", state.deck_id, e)
    finally:
        state.in_progress = False
