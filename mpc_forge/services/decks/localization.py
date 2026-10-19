from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import DeckCard, PrintingCache
from mpc_forge.services.cards.printings import upsert_printings
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)

PrintKey = tuple[str, str, str]


def _key(p: PrintingCache) -> PrintKey:
    return (p.oracle_id, p.set_code, p.collector_number)


def _localizable(p: PrintingCache | None) -> bool:
    return bool(p and p.oracle_id and p.set_code and p.collector_number)


async def _localized_candidates(
    db: AsyncSession, bases: list[PrintingCache], lang: str
) -> dict[PrintKey, PrintingCache]:
    oracle_ids = {b.oracle_id for b in bases if b.oracle_id}
    if not oracle_ids:
        return {}
    found: dict[PrintKey, PrintingCache] = {}
    for chunk in chunked(oracle_ids):
        rows = await db.scalars(
            select(PrintingCache).where(
                PrintingCache.oracle_id.in_(chunk), PrintingCache.lang == lang
            )
        )
        for row in rows:
            found.setdefault(_key(row), row)
    return found


async def _fetch_localized(
    scryfall: ScryfallClient, missing: list[PrintingCache], lang: str
) -> list[dict[str, Any]]:
    if not missing:
        return []
    try:
        return await scryfall.prints_in_lang(
            [(p.set_code, p.collector_number) for p in missing], lang
        )
    except Exception as e:
        log.warning("Localización a %s de %d cartas falló: %s", lang, len(missing), e)
        return []


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

    pending = [p for p in current_by_sfid.values() if p.lang != lang and _localizable(p)]
    candidates = await _localized_candidates(db, pending, lang)

    # Primero la red y después todas las escrituras: con la transacción abierta
    # durante las llamadas, el resto de escritores (y el volcado) esperarían
    # hasta el busy_timeout y fallarían con «database is locked».
    missing = list({_key(p): p for p in pending if _key(p) not in candidates}.values())
    fetched = await _fetch_localized(scryfall, missing, lang)
    for row in (await upsert_printings(db, fetched)).values():
        if row.lang == lang:
            candidates.setdefault(_key(row), row)

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
            candidates.get(_key(current)) if current and _localizable(current) else None
        )
        if localized_printing is None:
            unavailable.append(dc.name)
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
