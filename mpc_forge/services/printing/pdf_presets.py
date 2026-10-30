from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.errors import CodedError
from mpc_forge.models import KeyValue

KV_KEY = "pdf_studio.presets"
MAX_PRESETS = 30
MAX_NAME = 60
MAX_OPTIONS = 80
DECK_SPECIFIC = frozenset({"page_range"})

Scalar = str | int | float | bool | None


class PresetError(CodedError):
    code = "invalid_preset"


def _clean_name(name: str) -> str:
    cleaned = " ".join(str(name).split())[:MAX_NAME]
    if not cleaned:
        raise PresetError(code="invalid_preset_name")
    return cleaned


def _clean_opts(opts: dict[str, Any]) -> dict[str, Scalar]:
    out: dict[str, Scalar] = {}
    for key, value in opts.items():
        if key in DECK_SPECIFIC or not isinstance(key, str) or len(key) > 64:
            continue
        if (
            value is None
            or isinstance(value, bool | int | float)
            or (isinstance(value, str) and len(value) <= 200)
        ):
            out[key] = value
        if len(out) >= MAX_OPTIONS:
            break
    if not out:
        raise PresetError(code="invalid_preset")
    return out


async def list_presets(db: AsyncSession) -> list[dict[str, Any]]:
    row = await db.get(KeyValue, KV_KEY)
    if row is None:
        return []
    try:
        data = json.loads(row.value)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and isinstance(p.get("name"), str)]


async def _store(db: AsyncSession, presets: list[dict[str, Any]]) -> None:
    payload = json.dumps(presets, ensure_ascii=False)
    row = await db.get(KeyValue, KV_KEY)
    if row is None:
        db.add(KeyValue(key=KV_KEY, value=payload))
    else:
        row.value = payload
    await db.commit()


async def save_preset(db: AsyncSession, name: str, opts: dict[str, Any]) -> list[dict[str, Any]]:
    name = _clean_name(name)
    clean = _clean_opts(opts)
    presets = [p for p in await list_presets(db) if p["name"].casefold() != name.casefold()]
    if len(presets) >= MAX_PRESETS:
        raise PresetError(code="too_many_presets", limit=MAX_PRESETS)
    presets.append({"name": name, "opts": clean})
    presets.sort(key=lambda p: p["name"].casefold())
    await _store(db, presets)
    return presets


async def delete_preset(db: AsyncSession, name: str) -> bool:
    presets = await list_presets(db)
    kept = [p for p in presets if p["name"].casefold() != name.casefold()]
    if len(kept) == len(presets):
        return False
    await _store(db, kept)
    return True
