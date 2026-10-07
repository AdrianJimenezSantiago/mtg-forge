from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.schemas import (
    DeckCardView,
    DeckPriceView,
    DeckValidation,
    DeckView,
    IllegalCardView,
)
from mpc_forge.services.art import custom_art
from mpc_forge.services.decks import deck_covers, deck_validation
from mpc_forge.services.printing import history
from mpc_forge.utils.iterables import chunked

DFC_LAYOUTS = frozenset({"transform", "modal_dfc", "double_faced_token", "reversible_card"})
PRICED_ROLES = frozenset({"commander", "mainboard", "companion", "sideboard"})


async def _printings_by_id(
    db: AsyncSession, scryfall_ids: Iterable[str]
) -> dict[str, PrintingCache]:
    out: dict[str, PrintingCache] = {}
    for chunk in chunked(scryfall_ids):
        rows = (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all()
        out.update({p.scryfall_id: p for p in rows})
    return out


async def _customs_by_id(db: AsyncSession, custom_ids: Iterable[int]) -> dict[int, CustomArt]:
    out: dict[int, CustomArt] = {}
    for chunk in chunked(custom_ids):
        rows = (await db.scalars(select(CustomArt).where(CustomArt.id.in_(chunk)))).all()
        out.update({ca.id: ca for ca in rows})
    return out


async def _prints_count_by_oracle(db: AsyncSession, oracle_ids: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in chunked(oracle_ids):
        rows = (
            await db.execute(
                select(PrintingCache.oracle_id, func.count(PrintingCache.scryfall_id))
                .where(PrintingCache.oracle_id.in_(chunk))
                .group_by(PrintingCache.oracle_id)
            )
        ).all()
        for oid, n in rows:
            out[oid] = out.get(oid, 0) + int(n)
    return out


async def _custom_count_by_name(db: AsyncSession, name_norms: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in chunked(name_norms):
        rows = (
            await db.execute(
                select(CustomArt.card_name_normalized, func.count(CustomArt.id))
                .where(CustomArt.card_name_normalized.in_(chunk), CustomArt.face == "front")
                .group_by(CustomArt.card_name_normalized)
            )
        ).all()
        for name, n in rows:
            out[name] = out.get(name, 0) + int(n)
    return out


@dataclass(frozen=True)
class _CardContext:
    printings: dict[str, PrintingCache]
    customs: dict[int, CustomArt]
    prints_count: dict[str, int]
    custom_count: dict[str, int]
    history_stats: dict[str, history.CardHistoryStat]


async def _load_context(db: AsyncSession, cards: list[DeckCard]) -> _CardContext:
    oracle_ids = {c.oracle_id for c in cards if c.oracle_id}
    return _CardContext(
        printings=await _printings_by_id(db, {c.scryfall_id for c in cards}),
        customs=await _customs_by_id(
            db, {c.custom_art_front_id for c in cards if c.custom_art_front_id}
        ),
        prints_count=await _prints_count_by_oracle(db, oracle_ids),
        custom_count=await _custom_count_by_name(
            db, {custom_art.normalize_card_name(c.name) for c in cards}
        ),
        history_stats=await history.stats_for_oracle_ids(db, list(oracle_ids)),
    )


def _related_parts(printing: PrintingCache | None) -> list[dict[str, str]]:
    if not printing or not printing.related_parts:
        return []
    try:
        return json.loads(printing.related_parts)
    except (ValueError, TypeError):
        return []


def _card_view(dc: DeckCard, ctx: _CardContext, *, with_faces: bool) -> DeckCardView:
    printing = ctx.printings.get(dc.scryfall_id)
    thumb: str | None = None
    custom = ctx.customs.get(dc.custom_art_front_id) if dc.custom_art_front_id else None
    if custom:
        thumb = custom_art.custom_art_url(custom.relative_path)
    if not thumb:
        thumb = printing.image_normal if printing else None

    is_dfc = bool(printing and printing.layout in DFC_LAYOUTS)
    stat = ctx.history_stats.get(dc.oracle_id) if dc.oracle_id else None
    extra: dict = {}
    if with_faces:
        extra = {
            "back_thumbnail_url": printing.back_image_normal if is_dfc else None,
            "back_name": printing.back_name if is_dfc else None,
            "related_parts": _related_parts(printing),
        }

    return DeckCardView(
        id=dc.id,
        oracle_id=dc.oracle_id,
        name=dc.name,
        quantity=dc.quantity,
        scryfall_id=dc.scryfall_id,
        custom_art_front_id=dc.custom_art_front_id,
        custom_art_back_id=dc.custom_art_back_id,
        role=dc.role,
        include=dc.include,
        layout=printing.layout if printing else "normal",
        is_dfc=is_dfc,
        thumbnail_url=thumb,
        printings_available=ctx.prints_count.get(dc.oracle_id, 0) if dc.oracle_id else 0,
        custom_arts_available=ctx.custom_count.get(custom_art.normalize_card_name(dc.name), 0),
        history_copies=stat.total_copies if stat else 0,
        history_decks=stat.decks if stat else [],
        mana_cost=printing.mana_cost if printing else "",
        cmc=printing.cmc if printing else 0.0,
        type_line=printing.type_line if printing else "",
        colors=printing.colors.split(",") if printing and printing.colors else [],
        color_identity=(
            printing.color_identity.split(",") if printing and printing.color_identity else []
        ),
        rarity=printing.rarity if printing else "",
        keywords=[k for k in (printing.keywords or "").split(",") if k] if printing else [],
        **extra,
    )


async def _deckcards_to_views(db: AsyncSession, cards: list[DeckCard]) -> list[DeckCardView]:
    if not cards:
        return []
    ctx = await _load_context(db, cards)
    return [_card_view(dc, ctx, with_faces=False) for dc in cards]


async def _deckcard_to_view(db: AsyncSession, dc: DeckCard) -> DeckCardView:
    return (await _deckcards_to_views(db, [dc]))[0]


def _validation_view(val: deck_validation.DeckValidationResult, **extra) -> DeckValidation:
    return DeckValidation(
        format=val.format,
        expected=val.expected,
        counted=val.counted,
        is_valid=val.is_valid,
        message=val.message,
        level=val.level,
        breakdown=val.breakdown,
        **extra,
    )


async def _deck_to_view(db: AsyncSession, deck: Deck) -> DeckView:
    cards = list(
        (
            await db.scalars(
                select(DeckCard)
                .where(DeckCard.deck_id == deck.id)
                .order_by(DeckCard.role, DeckCard.name)
            )
        ).all()
    )
    base = {
        "id": deck.id,
        "name": deck.name,
        "moxfield_id": deck.moxfield_id,
        "source_url": deck.source_url,
        "format": deck.format,
        "commander_scryfall_id": deck.commander_scryfall_id,
        "imported_at": deck.imported_at,
        "updated_at": deck.updated_at,
    }
    if not cards:
        val = deck_validation.validate_deck(deck.format, [])
        return DeckView(**base, cards=[], validation=_validation_view(val))

    ctx = await _load_context(db, cards)
    card_views = [_card_view(dc, ctx, with_faces=True) for dc in cards]

    illegal = deck_validation.check_legalities(
        deck.format,
        [
            (
                c.name,
                c.role,
                getattr(ctx.printings.get(c.scryfall_id), "legalities", "") or "",
                c.include,
            )
            for c in cards
        ],
    )
    val = deck_validation.validate_deck(
        deck.format,
        [(c.role, c.quantity, c.include) for c in cards],
        illegal,
    )

    commanders = sorted(
        (c for c in cards if c.role == deck_covers.COMMANDER_ROLE), key=lambda c: c.id
    )
    cover_printings = ctx.printings
    wanted = deck.commander_scryfall_id
    if commanders and wanted and wanted not in ctx.printings:
        base_printing = await db.get(PrintingCache, wanted)
        if base_printing is not None:
            cover_printings = {**ctx.printings, wanted: base_printing}
    cover_card = deck_covers.pick_cover_card(deck, commanders, cover_printings)

    return DeckView(
        **base,
        cards=card_views,
        cover_card_id=cover_card.id if cover_card else None,
        validation=_validation_view(
            val,
            illegal=[
                IllegalCardView(name=c.name, status=c.status, role=c.role) for c in val.illegal
            ],
        ),
        price=_deck_price(cards, ctx.printings),
    )


def _deck_price(cards: list[DeckCard], printings_by_id: dict[str, PrintingCache]) -> DeckPriceView:
    total_eur = 0.0
    total_usd = 0.0
    priced = 0
    unpriced = 0
    for card in cards:
        if not card.include or card.role not in PRICED_ROLES:
            continue
        printing = printings_by_id.get(card.scryfall_id)
        eur = getattr(printing, "price_eur", None) if printing else None
        usd = getattr(printing, "price_usd", None) if printing else None
        if eur is None and usd is None:
            unpriced += card.quantity
            continue
        priced += card.quantity
        total_eur += (eur or 0.0) * card.quantity
        total_usd += (usd or 0.0) * card.quantity
    return DeckPriceView(
        eur=round(total_eur, 2),
        usd=round(total_usd, 2),
        priced_cards=priced,
        unpriced_cards=unpriced,
    )
