from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckCard, PrintingCache
from mpc_forge.services.decks import deck_validation

MAX_TAGS = 12
MAX_TAG_LENGTH = 32
COLOR_ORDER = "WUBRG"


def normalize_tags(raw: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in raw:
        tag = " ".join(str(value).split())[:MAX_TAG_LENGTH].strip()
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        out.append(tag)
        if len(out) == MAX_TAGS:
            break
    return out


def parse_tags(stored: str | None) -> list[str]:
    if not stored:
        return []
    try:
        value = json.loads(stored)
    except (ValueError, TypeError):
        return []
    return normalize_tags(value) if isinstance(value, list) else []


def serialize_tags(tags: Iterable[str]) -> str:
    return json.dumps(normalize_tags(tags), ensure_ascii=False)


def sort_colors(colors: Iterable[str]) -> str:
    present = {c.upper() for c in colors}
    return "".join(c for c in COLOR_ORDER if c in present)


@dataclass
class DeckStats:
    total: int = 0
    colors: str = ""
    custom_art: int = 0
    excluded: int = 0
    counted: int = 0
    expected: int = 0
    level: str = "ok"
    by_role: dict[str, int] = field(default_factory=dict)


async def stats_for_decks(db: AsyncSession, decks: Sequence[Deck]) -> dict[int, DeckStats]:
    if not decks:
        return {}
    ids = [d.id for d in decks]
    rows = (
        await db.execute(
            select(
                DeckCard.deck_id,
                DeckCard.role,
                DeckCard.quantity,
                DeckCard.include,
                DeckCard.custom_art_front_id,
                PrintingCache.color_identity,
            )
            .outerjoin(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id)
            .where(DeckCard.deck_id.in_(ids))
        )
    ).all()
    stats = {deck_id: DeckStats() for deck_id in ids}
    commander_colors: dict[int, set[str]] = {deck_id: set() for deck_id in ids}
    deck_colors: dict[int, set[str]] = {deck_id: set() for deck_id in ids}
    cards_for_validation: dict[int, list[tuple[str, int, bool]]] = {d: [] for d in ids}
    for deck_id, role, quantity, include, custom_front, identity in rows:
        s = stats[deck_id]
        cards_for_validation[deck_id].append((role, quantity, include))
        if not include:
            s.excluded += quantity
            continue
        s.total += quantity
        s.by_role[role] = s.by_role.get(role, 0) + quantity
        if custom_front:
            s.custom_art += 1
        colors = set((identity or "").upper()) & set(COLOR_ORDER)
        if role == "commander":
            commander_colors[deck_id] |= colors
        if role in ("commander", "mainboard", "companion"):
            deck_colors[deck_id] |= colors
    for deck in decks:
        s = stats[deck.id]
        s.colors = sort_colors(commander_colors[deck.id] or deck_colors[deck.id])
        val = deck_validation.validate_deck(deck.format, cards_for_validation[deck.id])
        s.counted, s.expected, s.level = val.counted, val.expected, val.level
    return stats


async def all_tags(db: AsyncSession) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for stored in (await db.scalars(select(Deck.tags))).all():
        for tag in parse_tags(stored):
            key = tag.casefold()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, tag)
    return [
        {"tag": display[k], "count": counts[k]}
        for k in sorted(counts, key=lambda k: (-counts[k], k))
    ]
