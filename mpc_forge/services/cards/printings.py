from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient, is_double_faced, related_parts_from_card
from mpc_forge.models import BulkSyncState, PrintingCache
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)


RESOLVE_CACHE_TTL = timedelta(days=7)


_NAME_MEMO_TTL = 24 * 3600.0


_NAME_MEMO_MAX = 20_000


_name_memo: dict[str, tuple[str, float]] = {}


_PRINTS_MEMO_TTL = 24 * 3600.0


_prints_complete: dict[str, float] = {}


PRINTS_BATCH_SIZE = 15


def normalize_card_name(name: str) -> str:
    # Scryfall ya no usa la ligadura: «Æther Vial» es hoy «Aether Vial».
    return (name or "").strip().lower().replace("’", "'").replace("`", "'").replace("æ", "ae")


def _name_keys(name: str) -> list[str]:
    full = normalize_card_name(name)
    keys = [full]
    if " // " in full:
        keys.extend(part.strip() for part in full.split(" // ") if part.strip())
    return keys


def _memo_name(name: str, scryfall_id: str) -> None:
    if len(_name_memo) >= _NAME_MEMO_MAX:
        _name_memo.clear()
    now = time.monotonic()
    for key in _name_keys(name):
        _name_memo.setdefault(key, (scryfall_id, now))


def _memo_lookup(name: str) -> str | None:
    hit = _name_memo.get(normalize_card_name(name))
    if not hit:
        return None
    sfid, at = hit
    if time.monotonic() - at > _NAME_MEMO_TTL:
        _name_memo.pop(normalize_card_name(name), None)
        return None
    return sfid


def _as_price(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def bulk_synced(db: AsyncSession) -> bool:
    return (await db.get(BulkSyncState, "default_cards")) is not None


def printing_fields(card: dict[str, Any]) -> dict[str, Any]:
    front_img = card.get("image_uris") or {}
    back_img: dict[str, str] = {}
    back_name = None

    mana_cost = card.get("mana_cost") or ""
    type_line = card.get("type_line") or ""
    colors = card.get("colors") or []

    if is_double_faced(card):
        faces = card.get("card_faces", [])
        front_img = faces[0].get("image_uris", front_img) if faces else front_img
        if faces:
            mana_cost = faces[0].get("mana_cost", mana_cost) or mana_cost
            type_line = faces[0].get("type_line", type_line) or type_line
            colors = faces[0].get("colors", colors) or colors
        if len(faces) > 1:
            back_img = faces[1].get("image_uris", {}) or {}
            back_name = faces[1].get("name")

    related_parts = related_parts_from_card(card)
    prices = card.get("prices") or {}
    return {
        "oracle_id": card.get("oracle_id") or "",
        "name": card.get("name", ""),
        "set_code": (card.get("set") or "").lower(),
        "set_name": card.get("set_name") or "",
        "collector_number": card.get("collector_number") or "",
        "rarity": card.get("rarity") or "",
        "lang": card.get("lang") or "en",
        "frame": card.get("frame") or "",
        "border_color": card.get("border_color") or "",
        "full_art": bool(card.get("full_art", False)),
        "textless": bool(card.get("textless", False)),
        "promo": bool(card.get("promo", False)),
        "digital": bool(card.get("digital", False)),
        "layout": card.get("layout") or "normal",
        "mana_cost": mana_cost,
        "cmc": float(card.get("cmc", 0.0) or 0.0),
        "type_line": type_line,
        "colors": ",".join(colors),
        "color_identity": ",".join(card.get("color_identity", []) or []),
        "keywords": ",".join(card.get("keywords", []) or []),
        "image_normal": front_img.get("normal"),
        "image_large": front_img.get("large"),
        "image_png": front_img.get("png"),
        "back_image_normal": back_img.get("normal") if back_img else None,
        "back_image_large": back_img.get("large") if back_img else None,
        "back_image_png": back_img.get("png") if back_img else None,
        "back_name": back_name,
        "artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "finishes": ",".join(card.get("finishes", []) or []),
        "price_usd": _as_price(prices.get("usd")),
        "price_usd_foil": _as_price(prices.get("usd_foil")),
        "price_eur": _as_price(prices.get("eur")),
        "legalities": (
            json.dumps(card.get("legalities") or {}, ensure_ascii=False)
            if card.get("legalities")
            else ""
        ),
        "related_parts": json.dumps(related_parts, ensure_ascii=False) if related_parts else "",
        "fetched_at": datetime.now(UTC),
    }


async def upsert_printing(db: AsyncSession, card: dict[str, Any]) -> PrintingCache:
    scryfall_id = card["id"]
    obj = await db.get(PrintingCache, scryfall_id)
    fields = printing_fields(card)
    if obj is None:
        obj = PrintingCache(scryfall_id=scryfall_id, **fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)
    await db.flush()
    return obj


async def upsert_printings(
    db: AsyncSession, cards: Iterable[dict[str, Any]]
) -> dict[str, PrintingCache]:
    by_id: dict[str, dict[str, Any]] = {}
    for c in cards:
        if c.get("id"):
            by_id[c["id"]] = c
    if not by_id:
        return {}

    existing: dict[str, PrintingCache] = {}
    for chunk in chunked(list(by_id)):
        for row in (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all():
            existing[row.scryfall_id] = row

    out: dict[str, PrintingCache] = {}
    for sfid, card in by_id.items():
        fields = printing_fields(card)
        obj = existing.get(sfid)
        if obj is None:
            obj = PrintingCache(scryfall_id=sfid, **fields)
            db.add(obj)
        else:
            for k, v in fields.items():
                setattr(obj, k, v)
        out[sfid] = obj
    await db.flush()
    return out


_NAME_CHUNK = 50


def _sql_lower_variants(key: str) -> set[str]:
    # lower() de SQLite sólo baja ASCII: «Éowyn» queda como «Éowyn», no «éowyn».
    return {key, "".join(c if c.isascii() else c.upper() for c in key)}


def _default_print_rank(row: Any, preferred_set: str | None, today: str) -> tuple[Any, ...]:
    return (
        bool(preferred_set) and row.set_code != preferred_set,
        (row.released_at or "") > today,
        bool(row.digital),
        bool(row.promo),
        row.border_color == "gold",
    )


async def find_local_by_names(
    db: AsyncSession, wanted: Iterable[tuple[str, str | None]]
) -> dict[tuple[str, str | None], str]:
    """(nombre normalizado, set preferido) → scryfall_id de la impresión que Scryfall
    daría por defecto.

    Sólo tiene sentido con el volcado importado: sin él la caché es parcial y
    elegir entre las impresiones que haya sería arbitrario.
    """
    wanted = set(wanted)
    names = {name for name, _ in wanted}
    if not names:
        return {}
    cols = (
        PrintingCache.scryfall_id,
        PrintingCache.name,
        PrintingCache.set_code,
        PrintingCache.digital,
        PrintingCache.promo,
        PrintingCache.border_color,
        PrintingCache.released_at,
    )
    candidates: dict[str, list[Any]] = {}
    lowered = func.lower(PrintingCache.name)
    for chunk in chunked({v for n in names for v in _sql_lower_variants(n)}):
        rows = await db.execute(select(*cols).where(lowered.in_(chunk), PrintingCache.lang == "en"))
        for row in rows:
            candidates.setdefault(normalize_card_name(row.name), []).append(row)

    # Cartas de dos caras/partidas: el decklist suele traer sólo la cara frontal.
    missing = names - candidates.keys()
    variants = {v for n in missing for v in _sql_lower_variants(n)}
    for chunk in chunked(variants, _NAME_CHUNK):
        ranges = [and_(lowered >= f"{k} // ", lowered < f"{k} /0") for k in chunk]
        rows = await db.execute(select(*cols).where(or_(*ranges), PrintingCache.lang == "en"))
        for row in rows:
            front = normalize_card_name(row.name).split(" // ", 1)[0]
            if front in missing:
                candidates.setdefault(front, []).append(row)

    today = datetime.now(UTC).date().isoformat()
    best: dict[tuple[str, str | None], str] = {}
    for name, preferred_set in wanted:
        rows = candidates.get(name)
        if rows:
            newest_first = sorted(rows, key=lambda r: r.released_at or "", reverse=True)
            pick = min(newest_first, key=lambda r: _default_print_rank(r, preferred_set, today))
            best[(name, preferred_set)] = pick.scryfall_id
    return best


async def find_local_printing(
    db: AsyncSession,
    *,
    name: str = "",
    set_code: str | None = None,
    collector_number: str | None = None,
) -> PrintingCache | None:
    if not await bulk_synced(db):
        return None
    if set_code and collector_number:
        return await db.scalar(
            select(PrintingCache).where(
                PrintingCache.set_code == set_code.lower(),
                PrintingCache.collector_number == collector_number,
                PrintingCache.lang == "en",
            )
        )
    key = (normalize_card_name(name), (set_code or "").lower() or None)
    if not key[0]:
        return None
    sfid = (await find_local_by_names(db, [key])).get(key)
    return await db.get(PrintingCache, sfid) if sfid else None


_AUTOCOMPLETE_LIMIT = 20

_NON_DECK_LAYOUTS = (
    "token",
    "double_faced_token",
    "emblem",
    "art_series",
    "planar",
    "scheme",
    "vanguard",
)


async def local_autocomplete(db: AsyncSession, query: str) -> list[str] | None:
    """Como /cards/autocomplete pero contra el volcado; None si no está importado."""
    if not await bulk_synced(db):
        return None
    q = normalize_card_name(query)
    if len(q) < 2:
        return []
    lowered = func.lower(PrintingCache.name)
    base = (
        select(PrintingCache.name)
        .where(
            PrintingCache.digital.is_(False),
            PrintingCache.layout.not_in(_NON_DECK_LAYOUTS),
        )
        .distinct()
        .order_by(PrintingCache.name)
        .limit(_AUTOCOMPLETE_LIMIT)
    )
    names = list((await db.scalars(base.where(lowered >= q, lowered < q + "\uffff"))).all())
    if len(names) < _AUTOCOMPLETE_LIMIT:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        more = await db.scalars(base.where(lowered.like(f"%{escaped}%", escape="\\")))
        names.extend(n for n in more if n not in names)
    return names[:_AUTOCOMPLETE_LIMIT]


def _name_set_key(entry: dict[str, Any]) -> tuple[str, str | None]:
    return normalize_card_name(entry.get("name", "")), (entry.get("set") or "").lower() or None


def _row_as_card(row: PrintingCache) -> dict[str, Any]:
    try:
        parts = json.loads(row.related_parts) if row.related_parts else []
    except (ValueError, TypeError):
        parts = []
    return {
        "id": row.scryfall_id,
        "oracle_id": row.oracle_id,
        "name": row.name,
        "set": row.set_code,
        "collector_number": row.collector_number,
        "layout": row.layout,
        "_related_parts": parts,
    }


def _meld_result_ids(card: dict[str, Any]) -> list[str]:
    if card.get("layout") != "meld":
        return []
    parts = card.get("_related_parts")
    if parts is None:
        parts = related_parts_from_card(card)
    return [p["id"] for p in parts if p.get("component") == "meld_result" and p.get("id")]


async def _resolve_from_cache(
    db: AsyncSession, entries: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    bulk = await bulk_synced(db)
    cutoff = datetime.now(UTC) - RESOLVE_CACHE_TTL

    def fresh(row: PrintingCache) -> bool:
        if not row.oracle_id:
            return False
        if bulk:
            return True
        fetched = _as_aware(row.fetched_at)
        return fetched is not None and fetched >= cutoff

    wanted_ids: set[str] = set()
    wanted_sets: set[str] = set()
    wanted_names: set[tuple[str, str | None]] = set()
    for e in entries:
        if e.get("scryfall_id"):
            wanted_ids.add(e["scryfall_id"])
        elif e.get("set") and e.get("number"):
            wanted_sets.add(e["set"])
        else:
            memo = _memo_lookup(e.get("name", ""))
            if memo:
                wanted_ids.add(memo)
            elif bulk and (key := _name_set_key(e))[0]:
                wanted_names.add(key)

    by_name = await find_local_by_names(db, wanted_names) if wanted_names else {}
    wanted_ids.update(by_name.values())

    by_id: dict[str, PrintingCache] = {}
    for chunk in chunked(list(wanted_ids)):
        for row in (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all():
            by_id[row.scryfall_id] = row

    by_set_num: dict[tuple[str, str], PrintingCache] = {}
    if wanted_sets:
        numbers = {e["number"] for e in entries if e.get("set") and e.get("number")}
        for chunk in chunked(list(numbers)):
            rows = (
                await db.scalars(
                    select(PrintingCache).where(
                        PrintingCache.set_code.in_(wanted_sets),
                        PrintingCache.collector_number.in_(chunk),
                        PrintingCache.lang == "en",
                    )
                )
            ).all()
            for row in rows:
                by_set_num[(row.set_code, row.collector_number)] = row

    cards: dict[str, dict[str, Any]] = {}
    hits: set[int] = set()
    for idx, e in enumerate(entries):
        cached: PrintingCache | None = None
        if e.get("scryfall_id"):
            cached = by_id.get(e["scryfall_id"])
        elif e.get("set") and e.get("number"):
            cached = by_set_num.get((e["set"], e["number"]))
        else:
            sfid = _memo_lookup(e.get("name", "")) or by_name.get(_name_set_key(e))
            cached = by_id.get(sfid) if sfid else None
        if cached is not None and fresh(cached):
            cards[f"idx:{idx}"] = _row_as_card(cached)
            hits.add(idx)
    return cards, hits


async def resolve_cards(
    db: AsyncSession, scryfall: ScryfallClient, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cached_cards, cache_hits = await _resolve_from_cache(db, entries)

    to_lookup: list[dict[str, str]] = []
    for idx, e in enumerate(entries):
        if idx in cache_hits:
            continue
        if e.get("scryfall_id"):
            to_lookup.append({"id": e["scryfall_id"]})
        elif e.get("set") and e.get("number"):
            to_lookup.append({"set": e["set"], "collector_number": e["number"]})
        else:
            to_lookup.append({"name": e["name"]})

    resolved_map: dict[str, dict[str, Any]] = {}
    meld_result_ids: set[str] = set()

    def index_card(c: dict[str, Any]) -> None:
        resolved_map[c["id"]] = c
        resolved_map[f"{c.get('set', '')}:{c.get('collector_number', '')}"] = c
        keys = _name_keys(c.get("name", ""))
        resolved_map[keys[0]] = c
        for k in keys[1:]:
            resolved_map.setdefault(k, c)
        meld_result_ids.update(_meld_result_ids(c))

    for c in cached_cards.values():
        meld_result_ids.update(_meld_result_ids(c))

    if to_lookup:
        cards = await scryfall.collection(to_lookup)
        await upsert_printings(db, cards)
        for c in cards:
            index_card(c)
            _memo_name(c.get("name", ""), c["id"])

    if meld_result_ids:
        have = set(
            (
                await db.scalars(
                    select(PrintingCache.scryfall_id).where(
                        PrintingCache.scryfall_id.in_(meld_result_ids)
                    )
                )
            ).all()
        )
        need = [{"id": sfid} for sfid in meld_result_ids if sfid not in have]
        if need:
            await upsert_printings(db, await scryfall.collection(need))

    out: list[dict[str, Any]] = []
    for idx, e in enumerate(entries):
        card: dict[str, Any] | None = cached_cards.get(f"idx:{idx}")
        if not card and e.get("scryfall_id"):
            card = resolved_map.get(e["scryfall_id"])
        if not card and e.get("set") and e.get("number"):
            card = resolved_map.get(f"{e['set']}:{e['number']}")
        if not card:
            card = resolved_map.get(normalize_card_name(e["name"]))
        if card:
            out.append(
                {
                    **e,
                    "scryfall_id": card["id"],
                    "oracle_id": card.get("oracle_id", ""),
                    "name": card.get("name", e["name"]),
                    "resolved": True,
                    "layout": card.get("layout", "normal"),
                }
            )
        else:
            out.append({**e, "resolved": False})
    await db.commit()
    if cache_hits:
        log.info(
            "Import: %d/%d entradas resueltas desde caché local, %d consultadas a Scryfall",
            len(cache_hits),
            len(entries),
            len(to_lookup),
        )
    return out


def _prints_known_complete(oracle_id: str) -> bool:
    at = _prints_complete.get(oracle_id)
    if at is None:
        return False
    if time.monotonic() - at > _PRINTS_MEMO_TTL:
        _prints_complete.pop(oracle_id, None)
        return False
    return True


async def fetch_printings_for_oracle(
    db: AsyncSession, scryfall: ScryfallClient, oracle_id: str
) -> list[PrintingCache]:
    stmt = (
        select(PrintingCache)
        .where(PrintingCache.oracle_id == oracle_id)
        .order_by(PrintingCache.released_at)
    )
    rows = list((await db.scalars(stmt)).all())
    if _prints_known_complete(oracle_id):
        return rows
    english = sum(1 for r in rows if (r.lang or "en") == "en")
    if english >= 2 or (rows and await bulk_synced(db)):
        return rows

    prints = await scryfall.prints_by_oracle_id(oracle_id)
    _prints_complete[oracle_id] = time.monotonic()
    if prints:
        await upsert_printings(db, prints)
        await db.commit()
        rows = list((await db.scalars(stmt)).all())
    return rows


async def pending_print_oracles(db: AsyncSession, oracle_ids: Iterable[str]) -> list[str]:
    ids = [o for o in dict.fromkeys(oracle_ids) if o and not _prints_known_complete(o)]
    if not ids:
        return []
    bulk = await bulk_synced(db)
    counts: dict[str, list[int]] = {}
    for chunk in chunked(ids):
        rows = await db.execute(
            select(PrintingCache.oracle_id, PrintingCache.lang).where(
                PrintingCache.oracle_id.in_(chunk)
            )
        )
        for oid, lang in rows:
            total_en = counts.setdefault(oid, [0, 0])
            total_en[0] += 1
            if (lang or "en") == "en":
                total_en[1] += 1
    pending = []
    for oid in ids:
        total, english = counts.get(oid, (0, 0))
        if english >= 2 or (total and bulk):
            continue
        pending.append(oid)
    return pending


async def store_prints(
    db: AsyncSession, oracle_ids: Iterable[str], prints: list[dict[str, Any]]
) -> None:
    now = time.monotonic()
    for oid in oracle_ids:
        _prints_complete[oid] = now
    if prints:
        await upsert_printings(db, prints)
        await db.commit()
