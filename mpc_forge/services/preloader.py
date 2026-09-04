"""Precarga en background de impresiones alternativas para las cartas de un mazo.

Cuando el usuario abre un mazo, arrancamos ``fetch_printings_for_oracle`` para
cada oracle_id, en background. Así cuando abre el modal de arte para cualquier
carta ya está cacheado y la respuesta es instantánea desde BD (sin llamar a
Scryfall).

Estado por ``deck_id``, cancelable, y expuesto vía ``to_dict()`` para que el
frontend haga polling y muestre progreso. La app corre en single-process, así
que un dict en memoria basta — no hace falta persistir en BD.

NOTA: este módulo se recreó para hacer arrancable el proyecto (el zip original
no lo incluía aunque ``routes/decks.py`` lo importa). Si tenías una versión más
completa en local, cópiala encima de este fichero.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import session_scope
from mpc_forge.models import DeckCard
from mpc_forge.services.deck_service import fetch_printings_for_oracle

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
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
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
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if state:
        state.in_progress = False


_PRELOAD_CONCURRENCY = 4


async def _run(state: PreloadState, scryfall: ScryfallClient) -> None:
    """Trae impresiones alternativas de cada oracle_id del mazo en paralelo.

    Cada tarea usa una sesión de BD propia — SQLAlchemy ``AsyncSession`` no
    soporta uso concurrente en la misma instancia, y aiosqlite serialize los
    writes a nivel de conexión, así que la concurrencia real se limita a las
    llamadas de red a Scryfall. Un semáforo pequeño evita saturar tanto el
    limitador del cliente como el pool de conexiones.
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
        state.total = len(oracle_ids)
        if not oracle_ids:
            return

        semaphore = asyncio.Semaphore(_PRELOAD_CONCURRENCY)

        async def _one(oid: str) -> None:
            async with semaphore:
                try:
                    async with session_scope() as task_db:
                        await fetch_printings_for_oracle(task_db, scryfall, oid)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.debug("Preload de oracle %s falló: %s", oid, e)
                finally:
                    state.done += 1

        await asyncio.gather(*(_one(oid) for oid in oracle_ids))
    except asyncio.CancelledError:
        log.debug("Preload del mazo %s cancelado", state.deck_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Preload del mazo %s falló: %s", state.deck_id, e)
    finally:
        state.in_progress = False
