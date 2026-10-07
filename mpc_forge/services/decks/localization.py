from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import DeckCard, PrintingCache
from mpc_forge.services.cards.printings import upsert_printing
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)


async def try_localize_card(
    db: AsyncSession,
    scryfall: ScryfallClient,
    scryfall_id: str,
    lang: str,
) -> PrintingCache | None:
    if lang == "en":
        return await db.get(PrintingCache, scryfall_id)

    base = await db.get(PrintingCache, scryfall_id)
    if not base or not base.set_code or not base.collector_number:
        return None
    if base.lang == lang:
        return base

    if base.oracle_id:
        candidates = (
            await db.scalars(
                select(PrintingCache).where(
                    PrintingCache.oracle_id == base.oracle_id,
                    PrintingCache.set_code == base.set_code,
                    PrintingCache.collector_number == base.collector_number,
                    PrintingCache.lang == lang,
                )
            )
        ).all()
        if candidates:
            return candidates[0]

    try:
        raw = await scryfall.by_set_and_number(base.set_code, base.collector_number, lang=lang)
    except Exception as e:
        log.debug(
            "Localización de %s/%s a %s falló: %s", base.set_code, base.collector_number, lang, e
        )
        return None
    if not raw:
        return None

    return await upsert_printing(db, raw)


async def _localized_candidates(
    db: AsyncSession, bases: list[PrintingCache], lang: str
) -> dict[tuple[str, str, str], PrintingCache]:
    oracle_ids = {b.oracle_id for b in bases if b.oracle_id}
    if not oracle_ids:
        return {}
    found: dict[tuple[str, str, str], PrintingCache] = {}
    for chunk in chunked(oracle_ids):
        rows = await db.scalars(
            select(PrintingCache).where(
                PrintingCache.oracle_id.in_(chunk), PrintingCache.lang == lang
            )
        )
        for row in rows:
            found.setdefault((row.oracle_id, row.set_code, row.collector_number), row)
    return found


async def localize_deck(
    db: AsyncSession,
    scryfall: ScryfallClient,
    deck_id: int,
    lang: str,
) -> dict[str, Any]:
    cards = (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck_id))).all()

    current_sfids = {dc.scryfall_id for dc in cards if not dc.custom_art_front_id}
    current_by_sfid: dict[str, PrintingCache] = {}
    for chunk in chunked(current_sfids):
        rows = await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        current_by_sfid.update({p.scryfall_id: p for p in rows})
    candidates = (
        await _localized_candidates(db, list(current_by_sfid.values()), lang)
        if lang != "en"
        else {}
    )

    localized = 0
    unchanged = 0
    unavailable: list[str] = []
    skipped_custom = 0

    for dc in cards:
        if dc.custom_art_front_id:
            skipped_custom += 1
            continue

        current = current_by_sfid.get(dc.scryfall_id)
        if current and current.lang == lang:
            unchanged += 1
            continue

        localized_printing = (
            candidates.get((current.oracle_id, current.set_code, current.collector_number))
            if current and current.oracle_id and current.set_code and current.collector_number
            else None
        )
        if localized_printing is None:
            localized_printing = await try_localize_card(db, scryfall, dc.scryfall_id, lang)
        if localized_printing is None:
            unavailable.append(dc.name)
            continue
        if localized_printing.scryfall_id == dc.scryfall_id:
            unchanged += 1
            continue
        dc.scryfall_id = localized_printing.scryfall_id
        localized += 1

    await db.commit()
    return {
        "lang": lang,
        "localized": localized,
        "unchanged": unchanged,
        "unavailable": unavailable,
        "skipped_custom": skipped_custom,
    }
