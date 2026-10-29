from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.errors import CodedError
from mpc_forge.models import CollectionEntry, PrintingCache
from mpc_forge.services.cards import images
from mpc_forge.services.cards.printings import resolve_cards
from mpc_forge.utils.iterables import chunked

ImportMode = Literal["merge", "replace"]

MAX_ROWS = 50_000
MAX_QUANTITY = 9_999
EXPORT_HEADER = ("Count", "Name", "Edition", "Collector Number", "Foil", "Scryfall ID")

_ALIASES: dict[str, frozenset[str]] = {
    "quantity": frozenset({"count", "quantity", "qty", "amount", "copies"}),
    "name": frozenset({"name", "card name", "card", "cardname"}),
    "set": frozenset({"edition", "set", "set code", "edition code", "setcode", "set_code"}),
    "number": frozenset(
        {"collector number", "card number", "collector_number", "number", "cn", "collector #"}
    ),
    "foil": frozenset({"foil", "finish", "printing", "foil/variant"}),
    "scryfall_id": frozenset({"scryfall id", "scryfall_id", "scryfallid", "scryfall uuid"}),
}
_FOIL_VALUES = frozenset({"foil", "etched", "true", "yes", "y", "1", "f"})


class CollectionImportError(CodedError):
    code = "collection_import_failed"


@dataclass
class CollectionRow:
    name: str
    quantity: int
    foil: bool
    set: str | None = None
    number: str | None = None
    scryfall_id: str | None = None
    line: int = 0


@dataclass
class ImportReport:
    rows: int = 0
    cards: int = 0
    printings: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unresolved: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "cards": self.cards,
            "printings": self.printings,
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "unresolved": self.unresolved,
        }


def _column_map(headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for header in headers:
        key = " ".join(str(header or "").strip().lower().replace("﻿", "").split())
        for field_name, aliases in _ALIASES.items():
            if key in aliases and field_name not in out:
                out[field_name] = header
    return out


def _quantity(raw: str | None) -> int:
    try:
        value = int(float(str(raw or "1").strip() or "1"))
    except ValueError:
        return 0
    return max(0, min(value, MAX_QUANTITY))


def _set_code(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    if not value or " " in value or len(value) > 6:
        return None
    return value


def parse_csv(text: str) -> list[CollectionRow]:
    sample = text[:4096]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = _column_map(list(reader.fieldnames or []))
    if "name" not in columns and "scryfall_id" not in columns:
        raise CollectionImportError(code="collection_csv_columns")
    rows: list[CollectionRow] = []
    for index, record in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            raise CollectionImportError(code="collection_csv_too_big", limit=MAX_ROWS)

        def get(name: str, rec: dict[str, Any] = record) -> str:
            col = columns.get(name)
            return str(rec.get(col) or "").strip() if col else ""

        name = get("name")
        scryfall_id = get("scryfall_id") or None
        if not name and not scryfall_id:
            continue
        quantity = _quantity(get("quantity")) if "quantity" in columns else 1
        if quantity <= 0:
            continue
        rows.append(
            CollectionRow(
                name=name,
                quantity=quantity,
                foil=get("foil").lower() in _FOIL_VALUES,
                set=_set_code(get("set")),
                number=get("number") or None,
                scryfall_id=scryfall_id,
                line=index,
            )
        )
    return rows


async def _resolve(
    db: AsyncSession, scryfall: ScryfallClient, rows: list[CollectionRow]
) -> list[tuple[CollectionRow, str | None]]:
    entries = [
        {
            "name": r.name or "",
            "quantity": r.quantity,
            "set": r.set if r.number else None,
            "number": r.number if r.set else None,
            "scryfall_id": r.scryfall_id,
        }
        for r in rows
    ]
    resolved = await resolve_cards(db, scryfall, entries) if entries else []
    ids: list[str | None] = [e["scryfall_id"] if e.get("resolved") else None for e in resolved]
    retry = [i for i, sid in enumerate(ids) if sid is None and rows[i].name]
    if retry:
        again = await resolve_cards(db, scryfall, [{"name": rows[i].name} for i in retry])
        for i, e in zip(retry, again, strict=True):
            if e.get("resolved"):
                ids[i] = e["scryfall_id"]
    return list(zip(rows, ids, strict=True))


async def import_csv(
    db: AsyncSession, scryfall: ScryfallClient, text: str, mode: ImportMode = "merge"
) -> ImportReport:
    rows = parse_csv(text)
    report = ImportReport(rows=len(rows))
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for parsed, sid in await _resolve(db, scryfall, rows):
        if sid is None:
            report.unresolved.append({"line": parsed.line, "name": parsed.name, "set": parsed.set})
            continue
        totals[sid][1 if parsed.foil else 0] += parsed.quantity

    printings: dict[str, PrintingCache] = {}
    for chunk in chunked(list(totals)):
        for cached in (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all():
            printings[cached.scryfall_id] = cached

    if mode == "replace":
        existing_count = len((await db.scalars(select(CollectionEntry.scryfall_id))).all())
        await db.execute(delete(CollectionEntry))
        report.removed = existing_count

    existing: dict[str, CollectionEntry] = {}
    for chunk in chunked(list(totals)):
        for row in (
            await db.scalars(select(CollectionEntry).where(CollectionEntry.scryfall_id.in_(chunk)))
        ).all():
            existing[row.scryfall_id] = row

    for sid, (normal, foil) in totals.items():
        report.cards += normal + foil
        entry = existing.get(sid)
        if entry is not None:
            entry.quantity = min(MAX_QUANTITY, (entry.quantity or 0) + normal)
            entry.foil_quantity = min(MAX_QUANTITY, (entry.foil_quantity or 0) + foil)
            report.updated += 1
            continue
        p = printings.get(sid)
        if p is None:
            continue
        db.add(
            CollectionEntry(
                scryfall_id=sid,
                oracle_id=p.oracle_id,
                name=p.name,
                set_code=p.set_code,
                set_name=p.set_name or "",
                collector_number=p.collector_number or "",
                rarity=p.rarity or "common",
                image_small=images.variant(p.image_normal, "thumb") or p.image_normal,
                quantity=normal,
                foil_quantity=foil,
            )
        )
        report.added += 1
    report.printings = len(totals)
    await db.commit()
    return report


async def export_csv(db: AsyncSession) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_HEADER)
    rows = (
        await db.scalars(
            select(CollectionEntry).order_by(
                CollectionEntry.set_code, CollectionEntry.collector_number, CollectionEntry.name
            )
        )
    ).all()
    for e in rows:
        base = [e.name, e.set_code, e.collector_number]
        if e.quantity:
            writer.writerow([e.quantity, *base, "", e.scryfall_id])
        if e.foil_quantity:
            writer.writerow([e.foil_quantity, *base, "foil", e.scryfall_id])
    return out.getvalue()


async def set_quantity(
    db: AsyncSession, entry_fields: dict[str, Any], quantity: int, foil_quantity: int
) -> CollectionEntry | None:
    sid = entry_fields["scryfall_id"]
    entry = await db.get(CollectionEntry, sid)
    quantity = max(0, min(quantity, MAX_QUANTITY))
    foil_quantity = max(0, min(foil_quantity, MAX_QUANTITY))
    if quantity == 0 and foil_quantity == 0:
        if entry is not None:
            await db.delete(entry)
        return None
    if entry is None:
        entry = CollectionEntry(**entry_fields)
        db.add(entry)
    entry.quantity = quantity
    entry.foil_quantity = foil_quantity
    return entry
