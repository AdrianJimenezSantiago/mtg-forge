from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.import_sites import resolve_site
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.errors import CodedError
from mpc_forge.models import Deck, DeckCard
from mpc_forge.services.cards.printings import resolve_cards
from mpc_forge.services.decks import deck_activity, importer, snapshots
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K
from mpc_forge.services.decks.decklist_parser import parse_plain_decklist

DECK_ROLES = frozenset({"commander", "mainboard", "companion", "sideboard", "maybeboard", "tokens"})
BulkAction = Literal["move", "include", "exclude", "delete"]


class DeckToolError(CodedError):
    code = "deck_tool_failed"


async def _cards(db: AsyncSession, deck_id: int, card_ids: Iterable[int]) -> list[DeckCard]:
    ids = set(card_ids)
    if not ids:
        return []
    rows = (
        await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck_id, DeckCard.id.in_(ids)))
    ).all()
    return list(rows)


async def bulk_edit(
    db: AsyncSession,
    deck_id: int,
    card_ids: list[int],
    action: BulkAction,
    role: str | None = None,
) -> int:
    """Aplica una acción a varias cartas, con un evento por carta para poder deshacerlas."""
    if action == "move" and role not in DECK_ROLES:
        raise DeckToolError(code="invalid_role", role=role or "")
    cards = await _cards(db, deck_id, card_ids)
    changed = 0
    for dc in cards:
        common = {
            "card_name": dc.name,
            "card_scryfall_id": dc.scryfall_id,
            "card_oracle_id": dc.oracle_id,
        }
        if action == "move":
            if dc.role == role:
                continue
            await deck_activity.log_event(
                db, deck_id, K.CARD_MOVED, payload={"from_role": dc.role, "to_role": role}, **common
            )
            dc.role = str(role)
        elif action in ("include", "exclude"):
            wanted = action == "include"
            if dc.include == wanted:
                continue
            dc.include = wanted
            await deck_activity.log_event(
                db,
                deck_id,
                K.CARD_INCLUDE_TOGGLED,
                payload={"new_include": wanted, "role": dc.role, "quantity": dc.quantity},
                **common,
            )
        else:
            await deck_activity.log_event(
                db,
                deck_id,
                K.CARD_REMOVED,
                payload={"quantity": dc.quantity, "role": dc.role},
                **common,
            )
            await db.delete(dc)
        changed += 1
    await db.commit()
    return changed


def _index(cards: Iterable[tuple[str, str, int, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for oracle_id, name, quantity, role in cards:
        key = oracle_id or name.lower()
        entry = out.setdefault(key, {"name": name, "quantity": 0, "roles": set()})
        entry["quantity"] += quantity
        entry["roles"].add(role)
    return out


async def _deck_index(db: AsyncSession, deck_id: int) -> dict[str, dict[str, Any]]:
    rows = (
        await db.execute(
            select(DeckCard.oracle_id, DeckCard.name, DeckCard.quantity, DeckCard.role).where(
                DeckCard.deck_id == deck_id,
                DeckCard.include.is_(True),
                DeckCard.role != "tokens",
            )
        )
    ).all()
    return _index((r[0], r[1], r[2], r[3]) for r in rows)


async def compare(db: AsyncSession, deck_a: Deck, deck_b: Deck) -> dict[str, Any]:
    a, b = await _deck_index(db, deck_a.id), await _deck_index(db, deck_b.id)

    def entry(key: str, source: dict[str, Any]) -> dict[str, Any]:
        return {"name": source["name"], "quantity": source["quantity"], "key": key}

    only_a = sorted((entry(k, a[k]) for k in a.keys() - b.keys()), key=lambda e: e["name"])
    only_b = sorted((entry(k, b[k]) for k in b.keys() - a.keys()), key=lambda e: e["name"])
    shared = a.keys() & b.keys()
    different = sorted(
        (
            {"name": a[k]["name"], "a": a[k]["quantity"], "b": b[k]["quantity"], "key": k}
            for k in shared
            if a[k]["quantity"] != b[k]["quantity"]
        ),
        key=lambda e: e["name"],
    )
    return {
        "a": {"id": deck_a.id, "name": deck_a.name},
        "b": {"id": deck_b.id, "name": deck_b.name},
        "only_a": only_a,
        "only_b": only_b,
        "different": different,
        "shared": len(shared),
        "shared_cards": sum(min(a[k]["quantity"], b[k]["quantity"]) for k in shared),
    }


@dataclass
class SyncChange:
    key: str
    name: str
    role: str
    change: Literal["added", "removed", "quantity"]
    before: int
    after: int
    scryfall_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SyncPlan:
    site: str
    changes: list[SyncChange]
    unresolved: list[dict[str, Any]]
    roles: list[str]
    _entries: dict[tuple[str, str], dict[str, Any]]


async def plan_sync(db: AsyncSession, scryfall: ScryfallClient, deck: Deck) -> SyncPlan:
    url = deck.source_url or ""
    site_cls = resolve_site(url) if url else None
    if site_cls is None:
        raise DeckToolError(code="no_source_url")
    text = await site_cls.retrieve_card_list(url)
    entries = parse_plain_decklist(text)
    for e in entries:
        e.setdefault("role", "mainboard")
    await importer._revert_dfc_backs_to_fronts(db, entries)
    resolved = await resolve_cards(db, scryfall, entries)
    unresolved = importer._unresolved_from_entries(resolved)

    current = (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck.id))).all()
    has_extras = any(c.role not in importer.CORE_ROLES and c.role != "tokens" for c in current)
    roles = sorted(DECK_ROLES - {"tokens"} if has_extras else importer.CORE_ROLES)

    remote: dict[tuple[str, str], dict[str, Any]] = {}
    for e in resolved:
        role = e.get("role", "mainboard")
        if not e.get("resolved") or role not in roles:
            continue
        key = (e.get("oracle_id") or e["name"].lower(), role)
        slot = remote.setdefault(key, {**e, "quantity": 0})
        slot["quantity"] += int(e.get("quantity", 1) or 1)

    local: dict[tuple[str, str], dict[str, Any]] = {}
    for c in current:
        if c.role not in roles:
            continue
        key = (c.oracle_id or c.name.lower(), c.role)
        slot = local.setdefault(key, {"name": c.name, "quantity": 0})
        slot["quantity"] += c.quantity

    changes: list[SyncChange] = []
    for key in remote.keys() - local.keys():
        r = remote[key]
        changes.append(
            SyncChange(
                f"{key[0]}|{key[1]}",
                r["name"],
                key[1],
                "added",
                0,
                r["quantity"],
                r.get("scryfall_id"),
            )
        )
    for key in local.keys() - remote.keys():
        lo = local[key]
        changes.append(
            SyncChange(f"{key[0]}|{key[1]}", lo["name"], key[1], "removed", lo["quantity"], 0)
        )
    for key in local.keys() & remote.keys():
        if local[key]["quantity"] != remote[key]["quantity"]:
            changes.append(
                SyncChange(
                    f"{key[0]}|{key[1]}",
                    local[key]["name"],
                    key[1],
                    "quantity",
                    local[key]["quantity"],
                    remote[key]["quantity"],
                )
            )
    changes.sort(key=lambda c: (c.change, c.role, c.name))
    return SyncPlan(site_cls.name, changes, unresolved, list(roles), remote)


async def apply_sync(
    db: AsyncSession, deck: Deck, plan: SyncPlan, keys: set[str] | None = None
) -> dict[str, Any]:
    chosen = [c for c in plan.changes if keys is None or c.key in keys]
    if not chosen:
        return {"applied": 0, "snapshot_id": None}
    snap = await snapshots.create(db, deck.id, label=f"{plan.site} sync", auto=True)
    current = list((await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck.id))).all())
    prefs = await importer._art_preferences(
        db, {c.key.split("|", 1)[0] for c in chosen if c.change == "added"}
    )
    counts = {"added": 0, "removed": 0, "quantity": 0}
    for change in chosen:
        oracle_or_name, role = change.key.split("|", 1)
        rows = [
            c
            for c in current
            if (c.oracle_id or c.name.lower()) == oracle_or_name and c.role == role
        ]
        if change.change == "removed":
            for row in rows:
                await db.delete(row)
        elif change.change == "quantity" and rows:
            rows[0].quantity = max(1, change.after - sum(r.quantity for r in rows[1:]))
        elif change.change == "added":
            entry = plan._entries.get((oracle_or_name, role))
            if entry is None:
                continue
            db.add(
                DeckCard(
                    deck_id=deck.id,
                    oracle_id=entry.get("oracle_id", ""),
                    name=entry["name"],
                    quantity=change.after,
                    scryfall_id=prefs.get(entry.get("oracle_id", ""), entry["scryfall_id"]),
                    role=role,
                    include=True,
                )
            )
        counts[change.change] += 1
    await deck_activity.log_event(
        db,
        deck.id,
        K.DECK_SYNCED,
        payload={"site": plan.site, **counts, "snapshot_id": snap["id"] if snap else None},
        deck_name=deck.name,
    )
    await db.commit()
    return {"applied": len(chosen), "snapshot_id": snap["id"] if snap else None, **counts}
