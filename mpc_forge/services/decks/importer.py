from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.import_sites import resolve_site
from mpc_forge.clients.import_sites.base import ImportSiteError
from mpc_forge.clients.moxfield import MoxfieldClient, normalize_deck
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import ArtPreference, Deck, DeckCard, DFCPair, PrintingCache
from mpc_forge.services.cards.printings import resolve_cards
from mpc_forge.services.decks.decklist_parser import parse_plain_decklist

log = logging.getLogger(__name__)


CORE_ROLES = frozenset({"commander", "mainboard"})


def _kept_entries(entries: list[dict[str, Any]], include_extras: bool) -> list[dict[str, Any]]:
    return [
        e
        for e in entries
        if e.get("resolved") and (include_extras or e.get("role", "mainboard") in CORE_ROLES)
    ]


def _meld_result_parts(printing: PrintingCache | None) -> list[dict[str, Any]]:
    if not printing or printing.layout != "meld" or not printing.related_parts:
        return []
    try:
        related = json.loads(printing.related_parts)
    except (ValueError, TypeError):
        return []
    return [part for part in related if part.get("component") == "meld_result"]


async def _printings_by_id(db: AsyncSession, scryfall_ids: set[str]) -> dict[str, PrintingCache]:
    if not scryfall_ids:
        return {}
    rows = (
        await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids)))
    ).all()
    return {p.scryfall_id: p for p in rows}


async def _art_preferences(db: AsyncSession, oracle_ids: set[str]) -> dict[str, str]:
    if not oracle_ids:
        return {}
    rows = (
        await db.execute(
            select(ArtPreference.oracle_id, ArtPreference.scryfall_id).where(
                ArtPreference.oracle_id.in_(oracle_ids)
            )
        )
    ).all()
    return dict(rows)


async def create_deck_from_entries(
    db: AsyncSession,
    name: str,
    entries: list[dict[str, Any]],
    moxfield_id: str | None = None,
    source_url: str | None = None,
    fmt: str = "commander",
    include_extras: bool = False,
) -> Deck:
    deck = Deck(name=name, moxfield_id=moxfield_id, source_url=source_url, format=fmt)
    db.add(deck)
    await db.flush()

    kept = _kept_entries(entries, include_extras)
    prefs_by_oracle = await _art_preferences(
        db, {e["oracle_id"] for e in kept if e.get("oracle_id")}
    )

    added_scryfall_ids: set[str] = set()
    for e in kept:
        role = e.get("role", "mainboard")
        oracle_id = e.get("oracle_id", "")
        chosen = prefs_by_oracle.get(oracle_id, e["scryfall_id"])
        db.add(
            DeckCard(
                deck_id=deck.id,
                oracle_id=oracle_id,
                name=e["name"],
                quantity=e["quantity"],
                scryfall_id=chosen,
                role=role,
                include=True,
            )
        )
        added_scryfall_ids.add(chosen)
        if role == "commander":
            deck.commander_scryfall_id = chosen

    printings_map = await _printings_by_id(db, added_scryfall_ids)
    meld_parts = [
        part for e in kept for part in _meld_result_parts(printings_map.get(e["scryfall_id"]))
    ]
    meld_printings = await _printings_by_id(db, {p["id"] for p in meld_parts if p.get("id")})

    meld_results_added: set[str] = set()
    for part in meld_parts:
        sfid = part.get("id")
        if not sfid or sfid in added_scryfall_ids or sfid in meld_results_added:
            continue
        cached = meld_printings.get(sfid)
        if not cached:
            continue
        db.add(
            DeckCard(
                deck_id=deck.id,
                oracle_id=cached.oracle_id or "",
                name=cached.name or part.get("name", ""),
                quantity=1,
                scryfall_id=sfid,
                role="tokens",
                include=True,
            )
        )
        meld_results_added.add(sfid)

    await db.commit()
    if meld_results_added:
        log.info("Auto-añadidos %d meld_result al mazo %s", len(meld_results_added), name)
    return deck


def _unresolved_from_entries(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in resolved:
        if e.get("resolved"):
            continue
        out.append(
            {
                "name": e.get("name", "") or "",
                "quantity": int(e.get("quantity", 1) or 1),
                "raw_line": e.get("raw_line"),
                "set": e.get("set"),
                "number": e.get("number"),
                "role": e.get("role", "mainboard"),
                "reason": "not_found_on_scryfall",
            }
        )
    return out


async def import_from_moxfield(
    db: AsyncSession,
    scryfall: ScryfallClient,
    mox: MoxfieldClient,
    url_or_id: str,
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    payload = await mox.fetch_deck(url_or_id)
    norm = normalize_deck(payload)
    resolved = await resolve_cards(db, scryfall, norm["cards"])
    unresolved = _unresolved_from_entries(resolved)
    deck = await create_deck_from_entries(
        db,
        name=norm["name"],
        entries=resolved,
        moxfield_id=norm["moxfield_id"],
        source_url=norm["source_url"],
        fmt=norm["format"],
        include_extras=include_extras,
    )
    return deck, unresolved


async def import_from_plaintext(
    db: AsyncSession,
    scryfall: ScryfallClient,
    name: str,
    text: str,
    fmt: str = "commander",
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    entries = parse_plain_decklist(text)
    for e in entries:
        e.setdefault("role", "mainboard")

    await _revert_dfc_backs_to_fronts(db, entries)

    resolved = await resolve_cards(db, scryfall, entries)
    unresolved = _unresolved_from_entries(resolved)
    deck = await create_deck_from_entries(
        db,
        name=name,
        entries=resolved,
        fmt=fmt,
        include_extras=include_extras,
    )
    return deck, unresolved


async def _revert_dfc_backs_to_fronts(
    db: AsyncSession,
    entries: list[dict[str, Any]],
) -> None:
    names_by_lower: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        n = (e.get("name") or "").strip()
        if not n or e.get("scryfall_id"):
            continue
        if " // " in n:
            continue
        names_by_lower.setdefault(n.lower(), []).append(e)
    if not names_by_lower:
        return

    rows = (
        await db.execute(
            select(DFCPair.front_name, DFCPair.back_name).where(
                func.lower(DFCPair.back_name).in_(list(names_by_lower.keys()))
            )
        )
    ).all()

    revert_count = 0
    for front, back in rows:
        for entry in names_by_lower.get(back.lower(), []):
            original = entry["name"]
            entry["dfc_reverted_from"] = original
            entry["name"] = front
            revert_count += 1

    if revert_count > 0:
        log.info(
            "DFC pre-processing: revertidos %d backs a fronts vía cache local",
            revert_count,
        )


async def import_from_url(
    db: AsyncSession,
    scryfall: ScryfallClient,
    url: str,
    name: str | None = None,
    fmt: str = "commander",
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    site_cls = resolve_site(url)
    if site_cls is None:
        raise ValueError(
            f"URL no soportada. Hostname no reconocido: {url!r}. "
            f"Sitios soportados: ver /api/decks/import/supported-sites"
        )

    text = await site_cls.retrieve_card_list(url)
    if not text.strip():
        raise ImportSiteError(f"{site_cls.name} devolvió una lista vacía")

    if not name:
        try:
            site_name = await site_cls.retrieve_deck_name(url)
        except Exception:
            site_name = None

        if site_name:
            name = site_name[:256]
        else:
            segments = [
                s for s in (urlparse(url).path or "").split("/") if s and s.lower() != "decks"
            ]
            tail = segments[-1] if segments else "imported"
            name = f"{site_cls.name} · {tail}"[:256]

    return await import_from_plaintext(
        db,
        scryfall,
        name=name,
        text=text,
        fmt=fmt,
        include_extras=include_extras,
    )
