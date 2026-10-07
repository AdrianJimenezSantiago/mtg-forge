from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import DFCPair, KeyValue

log = logging.getLogger(__name__)

SYNC_TTL = timedelta(days=7)

_LAST_SYNC_KEY = "dfc_pairs.last_synced_at"

_DFC_QUERY = (
    "is:dfc -layout:art_series -(layout:double_faced_token -keyword:transform) -is:reversible"
)
_MELD_QUERY = "is:meld"


async def _fetch_paginated(scryfall: ScryfallClient, query: str) -> list[dict[str, Any]]:
    return await scryfall.search_all(query, unique="cards")


def _dfc_pair_from_card(card: dict[str, Any]) -> DFCPair | None:
    faces = card.get("card_faces") or []
    if len(faces) < 2:
        return None
    front = faces[0].get("name")
    back = faces[1].get("name")
    if not front or not back:
        return None
    layout = card.get("layout", "transform")
    if layout not in {"transform", "modal_dfc"}:
        return None
    return DFCPair(
        front_name=front,
        back_name=back,
        kind=layout,
    )


def _meld_pairs_from_card(card: dict[str, Any]) -> list[DFCPair]:
    all_parts = card.get("all_parts") or []
    if not all_parts:
        return []
    self_id = card.get("id")
    self_name = card.get("name")

    meld_result = next(
        (p for p in all_parts if p.get("component") == "meld_result"),
        None,
    )
    if not meld_result:
        return []
    result_name = meld_result.get("name")
    if not result_name:
        return []

    is_self_meld_part = any(
        p.get("id") == self_id and p.get("component") == "meld_part" for p in all_parts
    )
    if not is_self_meld_part:
        return []

    oracle = card.get("oracle_text", "") or ""
    is_top = "\n(Melds with " not in oracle and "Melds with " not in oracle

    kind = "meld_top" if is_top else "meld_bottom"
    bit = "Top" if is_top else "Bottom"
    return [
        DFCPair(
            front_name=self_name,
            back_name=f"{result_name} {bit}",
            kind=kind,
        )
    ]


async def _fetch_all_pairs(scryfall: ScryfallClient) -> list[DFCPair]:
    pairs: dict[str, DFCPair] = {}

    dfc_cards = await _fetch_paginated(scryfall, _DFC_QUERY)
    for card in dfc_cards:
        if card.get("digital"):
            continue
        pair = _dfc_pair_from_card(card)
        if pair and pair.front_name not in pairs:
            pairs[pair.front_name] = pair

    meld_cards = await _fetch_paginated(scryfall, _MELD_QUERY)
    for card in meld_cards:
        if card.get("digital"):
            continue
        for pair in _meld_pairs_from_card(card):
            if pair.front_name not in pairs:
                pairs[pair.front_name] = pair

    return list(pairs.values())


async def _mark_synced(db: AsyncSession) -> None:
    now_iso = datetime.now(UTC).isoformat()
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    if kv:
        kv.value = now_iso
    else:
        db.add(KeyValue(key=_LAST_SYNC_KEY, value=now_iso))


async def _is_stale(db: AsyncSession) -> bool:
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    if not kv or not kv.value:
        return True
    try:
        last = datetime.fromisoformat(kv.value)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last) > SYNC_TTL


async def _count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count(DFCPair.id))) or 0)


async def sync_if_stale(db: AsyncSession, scryfall: ScryfallClient) -> dict[str, Any]:
    stale = await _is_stale(db)
    existing_count = await _count(db)
    if not stale and existing_count > 0:
        return {"synced": False, "pairs": existing_count, "reason": "up_to_date"}

    log.info(
        "Sincronizando DFC pairs desde Scryfall (existentes=%d, stale=%s)…", existing_count, stale
    )
    try:
        pairs = await _fetch_all_pairs(scryfall)
    except Exception as e:
        if existing_count > 0:
            log.warning("Fallo al sincronizar DFC pairs pero la tabla actual sigue viva: %s", e)
            return {"synced": False, "pairs": existing_count, "reason": f"error: {e!s}"}
        raise

    await db.execute(delete(DFCPair))
    db.add_all(pairs)
    await _mark_synced(db)
    await db.commit()

    log.info(
        "DFC pairs sincronizados: %d pares (%d DFCs regulares, %d meld pieces)",
        len(pairs),
        sum(1 for p in pairs if p.kind in {"transform", "modal_dfc"}),
        sum(1 for p in pairs if p.kind.startswith("meld_")),
    )
    return {"synced": True, "pairs": len(pairs), "reason": "refreshed"}


async def bulk_lookup(db: AsyncSession, names: list[str]) -> dict[str, dict[str, str]]:
    if not names:
        return {}
    lowered = [n.lower() for n in names if n]
    rows = (
        await db.execute(
            select(DFCPair.front_name, DFCPair.back_name, DFCPair.kind).where(
                func.lower(DFCPair.front_name).in_(lowered)
            )
        )
    ).all()
    by_lower: dict[str, tuple[str, str]] = {f.lower(): (b, k) for f, b, k in rows}
    out: dict[str, dict[str, str]] = {}
    for n in names:
        if not n:
            continue
        found = by_lower.get(n.lower())
        if found:
            out[n] = {"back_name": found[0], "kind": found[1]}
    return out


async def stats(db: AsyncSession) -> dict[str, Any]:
    total = await _count(db)
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    last = kv.value if kv else None
    return {
        "total_pairs": total,
        "last_synced_at": last,
    }
