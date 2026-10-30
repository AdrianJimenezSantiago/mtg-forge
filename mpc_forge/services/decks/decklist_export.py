from __future__ import annotations

import csv
import io
import re
from typing import Literal
from xml.sax.saxutils import quoteattr

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import Deck, DeckCard, PrintingCache

Format = Literal["simple", "with_set", "arena", "mtgo", "csv", "cockatrice"]

EXTENSIONS: dict[str, str] = {"csv": "csv", "cockatrice": "cod"}
MEDIA_TYPES: dict[str, str] = {
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "cod": "application/xml; charset=utf-8",
}

_ROLE_ORDER: list[tuple[str, str]] = [
    ("commander", "Commander"),
    ("mainboard", "Deck"),
    ("companion", "Companion"),
    ("sideboard", "Sideboard"),
    ("maybeboard", "Maybeboard"),
]
_MAIN_ROLES = ("commander", "mainboard")
_SIDE_ROLES = ("companion", "sideboard")

Row = tuple[DeckCard, PrintingCache | None]


async def _rows(db: AsyncSession, deck_id: int) -> list[Row]:
    result = await db.execute(
        select(DeckCard, PrintingCache)
        .join(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id, isouter=True)
        .where(DeckCard.deck_id == deck_id, DeckCard.include.is_(True))
        .order_by(DeckCard.role, DeckCard.name)
    )
    return [(dc, printing) for dc, printing in result.all()]


def _by_role(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for dc, printing in rows:
        out.setdefault(dc.role, []).append((dc, printing))
    return out


async def build_decklist_text(
    db: AsyncSession,
    deck_id: int,
    fmt: Format = "with_set",
    include_headers: bool = True,
) -> str:
    rows = await _rows(db, deck_id)
    if not rows:
        return ""
    if fmt == "csv":
        return _as_csv(rows)
    if fmt == "cockatrice":
        deck = await db.get(Deck, deck_id)
        return _as_cockatrice(rows, deck.name if deck else "", deck.notes if deck else "")
    if fmt == "mtgo":
        return _as_mtgo(rows)

    by_role = _by_role(rows)
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


def count_cards(rows_text: str, fmt: Format) -> int:
    if fmt == "csv":
        reader = csv.DictReader(io.StringIO(rows_text))
        return sum(int(r.get("Count") or 0) for r in reader)
    if fmt == "cockatrice":
        return sum(int(n) for n in re.findall(r'<card number="(\d+)"', rows_text))
    return sum(
        int(line.split(" ", 1)[0])
        for line in rows_text.splitlines()
        if line and line.split(" ", 1)[0].isdigit()
    )


def _as_mtgo(rows: list[Row]) -> str:
    by_role = _by_role(rows)
    main = [f"{dc.quantity} {dc.name}" for role in _MAIN_ROLES for dc, _ in by_role.get(role, [])]
    side = [f"{dc.quantity} {dc.name}" for role in _SIDE_ROLES for dc, _ in by_role.get(role, [])]
    text = "\n".join(main)
    if side:
        text += "\n\n" + "\n".join(side)
    return text + "\n"


def _as_csv(rows: list[Row]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Count", "Name", "Edition", "Collector Number", "Board", "Scryfall ID"])
    order = {role: i for i, (role, _) in enumerate(_ROLE_ORDER)}
    for dc, printing in sorted(rows, key=lambda r: (order.get(r[0].role, 99), r[0].name)):
        writer.writerow(
            [
                dc.quantity,
                dc.name,
                printing.set_code if printing else "",
                printing.collector_number if printing else "",
                dc.role,
                dc.scryfall_id,
            ]
        )
    return out.getvalue()


def _as_cockatrice(rows: list[Row], name: str, notes: str | None) -> str:
    by_role = _by_role(rows)
    zones = {
        "main": [r for role in _MAIN_ROLES for r in by_role.get(role, [])],
        "side": [r for role in _SIDE_ROLES for r in by_role.get(role, [])],
        "tokens": by_role.get("tokens", []),
    }
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<cockatrice_deck version="1">',
        f"  <deckname>{_xml_text(name)}</deckname>",
        f"  <comments>{_xml_text(notes or '')}</comments>",
    ]
    for zone, cards in zones.items():
        if not cards:
            continue
        lines.append(f"  <zone name={quoteattr(zone)}>")
        lines.extend(
            f'    <card number="{dc.quantity}" name={quoteattr(dc.name)}/>' for dc, _ in cards
        )
        lines.append("  </zone>")
    lines.append("</cockatrice_deck>")
    return "\n".join(lines) + "\n"


def _xml_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def extension_for(fmt: Format) -> str:
    return EXTENSIONS.get(fmt, "txt")


def filename_for(deck_name: str, fmt: Format) -> str:
    slug = slugify(deck_name) or "deck"
    return f"{slug}-{fmt}.{extension_for(fmt)}"
