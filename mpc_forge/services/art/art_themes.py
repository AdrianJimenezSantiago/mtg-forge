from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import ArtTheme, ArtThemeEntry, Deck, DeckCard

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    theme_id: int
    theme_name: str
    deck_id: int
    changed: int = 0
    already_matching: int = 0
    not_in_theme: int = 0
    missing_art: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme_id": self.theme_id,
            "theme_name": self.theme_name,
            "deck_id": self.deck_id,
            "changed": self.changed,
            "already_matching": self.already_matching,
            "not_in_theme": self.not_in_theme,
            "missing_art": self.missing_art,
        }


async def list_themes(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (await db.execute(select(ArtTheme).order_by(ArtTheme.created_at.desc()))).scalars().all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "entry_count": t.entry_count,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


async def get_theme(db: AsyncSession, theme_id: int) -> dict[str, Any] | None:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return None
    entries = (
        (
            await db.execute(
                select(ArtThemeEntry)
                .where(ArtThemeEntry.theme_id == theme_id)
                .order_by(ArtThemeEntry.card_name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "id": theme.id,
        "name": theme.name,
        "description": theme.description,
        "entry_count": theme.entry_count,
        "created_at": theme.created_at.isoformat() if theme.created_at else None,
        "entries": [
            {
                "oracle_id": e.oracle_id,
                "card_name": e.card_name,
                "scryfall_id": e.scryfall_id,
                "custom_art_front_id": e.custom_art_front_id,
                "custom_art_back_id": e.custom_art_back_id,
            }
            for e in entries
        ],
    }


async def create_from_deck(
    db: AsyncSession,
    deck_id: int,
    name: str,
    *,
    description: str = "",
    only_customized: bool = True,
) -> dict[str, Any] | None:
    deck = await db.get(Deck, deck_id)
    if deck is None:
        return None

    cards = (await db.execute(select(DeckCard).where(DeckCard.deck_id == deck_id))).scalars().all()

    theme = ArtTheme(name=name.strip() or "Tema sin nombre", description=description)
    db.add(theme)
    await db.flush()

    seen: set[str] = set()
    count = 0
    for card in cards:
        has_custom = card.custom_art_front_id is not None or card.custom_art_back_id is not None
        if only_customized and not has_custom:
            continue
        if not card.oracle_id or card.oracle_id in seen:
            continue
        seen.add(card.oracle_id)

        db.add(
            ArtThemeEntry(
                theme_id=theme.id,
                oracle_id=card.oracle_id,
                card_name=card.name,
                scryfall_id=card.scryfall_id,
                custom_art_front_id=card.custom_art_front_id,
                custom_art_back_id=card.custom_art_back_id,
            )
        )
        count += 1

    theme.entry_count = count
    await db.commit()
    log.info("Tema '%s' creado con %d entradas desde el mazo %d", theme.name, count, deck_id)
    return await get_theme(db, theme.id)


async def apply_to_deck(
    db: AsyncSession,
    theme_id: int,
    deck_id: int,
    *,
    overwrite_custom: bool = True,
) -> ApplyResult | None:
    theme = await db.get(ArtTheme, theme_id)
    deck = await db.get(Deck, deck_id)
    if theme is None or deck is None:
        return None

    entries = (
        (await db.execute(select(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id)))
        .scalars()
        .all()
    )
    by_oracle = {e.oracle_id: e for e in entries}

    cards = (await db.execute(select(DeckCard).where(DeckCard.deck_id == deck_id))).scalars().all()

    result = ApplyResult(theme_id=theme.id, theme_name=theme.name, deck_id=deck_id)

    for card in cards:
        entry = by_oracle.get(card.oracle_id)
        if entry is None:
            result.not_in_theme += 1
            continue

        has_custom = card.custom_art_front_id is not None or card.custom_art_back_id is not None
        if has_custom and not overwrite_custom:
            result.already_matching += 1
            continue

        unchanged = (
            card.scryfall_id == (entry.scryfall_id or card.scryfall_id)
            and card.custom_art_front_id == entry.custom_art_front_id
            and card.custom_art_back_id == entry.custom_art_back_id
        )
        if unchanged:
            result.already_matching += 1
            continue

        if entry.scryfall_id:
            card.scryfall_id = entry.scryfall_id
        card.custom_art_front_id = entry.custom_art_front_id
        card.custom_art_back_id = entry.custom_art_back_id
        result.changed += 1

    await db.commit()
    log.info(
        "Tema '%s' aplicado al mazo %d: %d cartas cambiadas", theme.name, deck_id, result.changed
    )
    return result


async def preview_apply(db: AsyncSession, theme_id: int, deck_id: int) -> dict[str, Any] | None:
    theme = await db.get(ArtTheme, theme_id)
    deck = await db.get(Deck, deck_id)
    if theme is None or deck is None:
        return None

    entries = (
        (await db.execute(select(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id)))
        .scalars()
        .all()
    )
    by_oracle = {e.oracle_id: e for e in entries}

    cards = (await db.execute(select(DeckCard).where(DeckCard.deck_id == deck_id))).scalars().all()

    would_change = []
    for card in cards:
        entry = by_oracle.get(card.oracle_id)
        if entry is None:
            continue
        unchanged = (
            card.scryfall_id == (entry.scryfall_id or card.scryfall_id)
            and card.custom_art_front_id == entry.custom_art_front_id
            and card.custom_art_back_id == entry.custom_art_back_id
        )
        if unchanged:
            continue
        would_change.append(
            {
                "card_id": card.id,
                "name": card.name,
                "from_scryfall_id": card.scryfall_id,
                "to_scryfall_id": entry.scryfall_id,
                "to_custom_front": entry.custom_art_front_id,
            }
        )

    return {
        "theme_id": theme.id,
        "theme_name": theme.name,
        "deck_id": deck_id,
        "deck_name": deck.name,
        "would_change": len(would_change),
        "unaffected": len(cards) - len(would_change),
        "cards": would_change[:100],
    }


async def delete_theme(db: AsyncSession, theme_id: int) -> bool:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return False
    await db.execute(delete(ArtThemeEntry).where(ArtThemeEntry.theme_id == theme_id))
    await db.delete(theme)
    await db.commit()
    return True


async def rename_theme(
    db: AsyncSession, theme_id: int, name: str, description: str | None = None
) -> dict[str, Any] | None:
    theme = await db.get(ArtTheme, theme_id)
    if theme is None:
        return None
    if name.strip():
        theme.name = name.strip()
    if description is not None:
        theme.description = description
    await db.commit()
    return await get_theme(db, theme_id)
