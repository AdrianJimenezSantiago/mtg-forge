from __future__ import annotations

from typing import Literal

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import DeckCard, PrintingCache

Format = Literal["simple", "with_set", "arena"]


_ROLE_ORDER: list[tuple[str, str]] = [
    ("commander", "Commander"),
    ("mainboard", "Deck"),
    ("companion", "Companion"),
    ("sideboard", "Sideboard"),
    ("maybeboard", "Maybeboard"),
]


async def build_decklist_text(
    db: AsyncSession,
    deck_id: int,
    fmt: Format = "with_set",
    include_headers: bool = True,
) -> str:
    rows = (
        await db.execute(
            select(DeckCard, PrintingCache)
            .join(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id, isouter=True)
            .where(DeckCard.deck_id == deck_id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()

    if not rows:
        return ""

    by_role: dict[str, list[tuple[DeckCard, PrintingCache | None]]] = {}
    for dc, printing in rows:
        by_role.setdefault(dc.role, []).append((dc, printing))

    lines: list[str] = []
    for role_key, role_label in _ROLE_ORDER:
        group = by_role.get(role_key)
        if not group:
            continue
        if include_headers:
            if lines:
                lines.append("")
            lines.append(role_label)
        for card, cached in group:
            lines.append(_format_line(card, cached, fmt))

    return "\n".join(lines) + "\n"


def _format_line(dc: DeckCard, printing: PrintingCache | None, fmt: Format) -> str:
    name = dc.name
    qty = dc.quantity

    if fmt == "simple" or printing is None:
        return f"{qty} {name}"

    set_code = printing.set_code or ""
    number = printing.collector_number or ""

    if not set_code or not number:
        return f"{qty} {name}"

    if fmt == "arena":
        return f"{qty} {name} ({set_code.upper()}) {number}"

    return f"{qty} {name} ({set_code.lower()}) {number}"


def filename_for(deck_name: str, fmt: Format) -> str:
    slug = slugify(deck_name) or "deck"
    return f"{slug}-{fmt}.txt"
