"""Estado del tutorial guiado: qué guías ha visto ya el usuario.

Se guarda en la base de datos (no en localStorage) para que la detección de
"primera vez" sobreviva a la ventana de escritorio, que puede no conservar el
almacenamiento del navegador entre sesiones.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import KeyValue

KEY = "ui.tour"
TOUR_ID = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
MAX_SEEN = 64


def _empty() -> dict[str, Any]:
    return {"seen": [], "auto": True}


async def get_state(db: AsyncSession) -> dict[str, Any]:
    kv = await db.get(KeyValue, KEY)
    if not kv or not kv.value:
        return _empty()
    try:
        raw = json.loads(kv.value)
    except ValueError:
        return _empty()
    seen = [s for s in raw.get("seen", []) if isinstance(s, str) and TOUR_ID.match(s)]
    return {"seen": seen[:MAX_SEEN], "auto": bool(raw.get("auto", True))}


async def _save(db: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    value = json.dumps(state)
    kv = await db.get(KeyValue, KEY)
    if kv:
        kv.value = value
    else:
        db.add(KeyValue(key=KEY, value=value))
    await db.commit()
    return state


async def mark_seen(db: AsyncSession, tour: str) -> dict[str, Any]:
    if not TOUR_ID.match(tour):
        raise ValueError(f"Identificador de guía no válido: {tour!r}")
    state = await get_state(db)
    if tour not in state["seen"]:
        state["seen"] = [*state["seen"], tour][-MAX_SEEN:]
    return await _save(db, state)


async def set_auto(db: AsyncSession, auto: bool) -> dict[str, Any]:
    state = await get_state(db)
    state["auto"] = auto
    return await _save(db, state)


async def reset(db: AsyncSession) -> dict[str, Any]:
    return await _save(db, _empty())
