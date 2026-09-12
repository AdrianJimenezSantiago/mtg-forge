"""Serializadores compartidos: modelos ORM → esquemas de respuesta.

Estas funciones las usan casi todos los sub-routers de mazos (`crud`,
`imports`, `tokens`, `art`), así que viven aquí en lugar de en cualquiera de
ellos. Si estuvieran dentro de un sub-router, los demás tendrían que importarlo
y aparecerían ciclos de importación en cuanto ese router importara algo de
vuelta.

Extraído de `routes/decks.py` durante la división en sub-routers; la lógica es
la original sin cambios.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TypeVar

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
from mpc_forge.services import custom_art, deck_validation, history

log = logging.getLogger(__name__)

T = TypeVar("T")

# Máximo de valores por cláusula ``IN``. SQLite compila cada elemento como un
# parámetro y tiene un tope (`SQLITE_MAX_VARIABLE_NUMBER`, históricamente 999);
# pasarse lanza "too many SQL variables" en tiempo de ejecución.
#
# Con un mazo normal da igual, pero un cubo importado de CubeCobra pasa de 540
# cartas y un mazo con muchas impresiones distintas se acerca rápido. Mismo
# valor que ``deck_service._IN_CHUNK`` para no tener dos criterios distintos.
_IN_CHUNK = 500


def _chunks(items: Iterable[T], size: int = _IN_CHUNK) -> Iterable[list[T]]:
    seq = list(items)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def _printings_by_id(
    db: AsyncSession, scryfall_ids: Iterable[str]
) -> dict[str, PrintingCache]:
    """Printings indexados por scryfall_id, troceando el ``IN``."""
    out: dict[str, PrintingCache] = {}
    for chunk in _chunks(scryfall_ids):
        if not chunk:
            continue
        rows = (await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk))
        )).all()
        out.update({p.scryfall_id: p for p in rows})
    return out


async def _customs_by_id(
    db: AsyncSession, custom_ids: Iterable[int]
) -> dict[int, CustomArt]:
    out: dict[int, CustomArt] = {}
    for chunk in _chunks(custom_ids):
        if not chunk:
            continue
        rows = (await db.scalars(
            select(CustomArt).where(CustomArt.id.in_(chunk))
        )).all()
        out.update({ca.id: ca for ca in rows})
    return out


async def _prints_count_by_oracle(
    db: AsyncSession, oracle_ids: Iterable[str]
) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in _chunks(oracle_ids):
        if not chunk:
            continue
        rows = (await db.execute(
            select(PrintingCache.oracle_id, func.count(PrintingCache.scryfall_id))
            .where(PrintingCache.oracle_id.in_(chunk))
            .group_by(PrintingCache.oracle_id)
        )).all()
        for oid, n in rows:
            out[oid] = out.get(oid, 0) + int(n)
    return out


async def _custom_count_by_name(
    db: AsyncSession, name_norms: Iterable[str]
) -> dict[str, int]:
    out: dict[str, int] = {}
    for chunk in _chunks(name_norms):
        if not chunk:
            continue
        rows = (await db.execute(
            select(CustomArt.card_name_normalized, func.count(CustomArt.id))
            .where(
                CustomArt.card_name_normalized.in_(chunk),
                CustomArt.face == "front",
            )
            .group_by(CustomArt.card_name_normalized)
        )).all()
        for name, c in rows:
            out[name] = out.get(name, 0) + int(c)
    return out


async def _deckcards_to_views(db: AsyncSession, cards: list[DeckCard]) -> list[DeckCardView]:
    """Versión BATCH para listas de DeckCards (usada por endpoints que devuelven
    varias cartas: tokens_add_many, add_related_cards, etc.).

    Antes: [await _deckcard_to_view(db, dc) for dc in cards] → 4*N queries.
    Ahora: 5 queries fijas independientemente de N. Reutiliza el mismo patrón
    que `_deck_to_view` pero sin necesitar la validación del mazo.
    """
    if not cards:
        return []

    # --- BATCH 1: printings ---
    scryfall_ids = {c.scryfall_id for c in cards}
    printings_by_id = await _printings_by_id(db, scryfall_ids)

    # --- BATCH 2: custom arts frontales ---
    custom_ids = {c.custom_art_front_id for c in cards if c.custom_art_front_id}
    customs_by_id = await _customs_by_id(db, custom_ids)

    # --- BATCH 3: nº total de impresiones por oracle_id ---
    oracle_ids = {c.oracle_id for c in cards if c.oracle_id}
    prints_count_by_oracle = await _prints_count_by_oracle(db, oracle_ids)

    # --- BATCH 4: nº de custom arts disponibles por nombre normalizado ---
    name_norms = {custom_art.normalize_card_name(c.name) for c in cards}
    custom_count_by_name = await _custom_count_by_name(db, name_norms)

    # --- BATCH 5: history agregado ---
    stats_map = await history.stats_for_oracle_ids(db, list(oracle_ids))

    # --- Composición sin más queries ---
    out: list[DeckCardView] = []
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        thumb: str | None = None
        if dc.custom_art_front_id:
            ca = customs_by_id.get(dc.custom_art_front_id)
            if ca:
                thumb = custom_art.custom_art_url(ca.relative_path)
        if not thumb:
            thumb = printing.image_normal if printing else None

        is_dfc = bool(printing and printing.layout in _DFC_LAYOUTS)
        stat = stats_map.get(dc.oracle_id) if dc.oracle_id else None
        out.append(DeckCardView(
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
            printings_available=prints_count_by_oracle.get(dc.oracle_id, 0),
            custom_arts_available=custom_count_by_name.get(
                custom_art.normalize_card_name(dc.name), 0
            ),
            history_copies=stat.total_copies if stat else 0,
            history_decks=stat.decks if stat else [],
            mana_cost=printing.mana_cost if printing else "",
            cmc=printing.cmc if printing else 0.0,
            type_line=printing.type_line if printing else "",
            colors=printing.colors.split(",") if printing and printing.colors else [],
            color_identity=printing.color_identity.split(",") if printing and printing.color_identity else [],
            rarity=printing.rarity if printing else "",
            keywords=[k for k in (printing.keywords or "").split(",") if k] if printing else [],
        ))
    return out


async def _deckcard_to_view(db: AsyncSession, dc: DeckCard) -> DeckCardView:
    """Versión single-card (para endpoints que devuelven UNA sola carta).

    ATENCIÓN: no llamar a este método en un bucle ``for``. Hace ~5 queries
    por carta y regresaríamos al N+1 que ya está resuelto. Para lotes de
    cartas usa :func:`_deckcards_to_views` (5 queries fijas), y para vistas
    completas de mazo :func:`_deck_to_view`.
    """
    printing = await db.get(PrintingCache, dc.scryfall_id)
    thumb: str | None = None
    if dc.custom_art_front_id:
        ca = await db.get(CustomArt, dc.custom_art_front_id)
        if ca:
            thumb = custom_art.custom_art_url(ca.relative_path)
    if not thumb:
        thumb = printing.image_normal if printing else None

    is_dfc = False
    if printing:
        is_dfc = printing.layout in {
            "transform", "modal_dfc", "double_faced_token", "reversible_card"
        }

    prints_count = 0
    if dc.oracle_id:
        prints_count = int(
            await db.scalar(
                select(func.count(PrintingCache.scryfall_id)).where(
                    PrintingCache.oracle_id == dc.oracle_id
                )
            ) or 0
        )
    custom_count = int(
        await db.scalar(
            select(func.count(CustomArt.id)).where(
                CustomArt.card_name_normalized == custom_art.normalize_card_name(dc.name),
                CustomArt.face == "front",
            )
        ) or 0
    )
    stats_map = await history.stats_for_oracle_ids(db, [dc.oracle_id]) if dc.oracle_id else {}
    stat = stats_map.get(dc.oracle_id)

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
        printings_available=prints_count,
        custom_arts_available=custom_count,
        history_copies=stat.total_copies if stat else 0,
        history_decks=stat.decks if stat else [],
        mana_cost=printing.mana_cost if printing else "",
        cmc=printing.cmc if printing else 0.0,
        type_line=printing.type_line if printing else "",
        colors=printing.colors.split(",") if printing and printing.colors else [],
        color_identity=printing.color_identity.split(",") if printing and printing.color_identity else [],
        rarity=printing.rarity if printing else "",
        keywords=[k for k in (printing.keywords or "").split(",") if k] if printing else [],
    )


_DFC_LAYOUTS = {"transform", "modal_dfc", "double_faced_token", "reversible_card"}


async def _deck_to_view(db: AsyncSession, deck: Deck) -> DeckView:
    """Vista completa del mazo con TODOS los datos precargados en batch.

    Solución al N+1: en lugar de ~6 queries por carta (600 para un mazo commander),
    hacemos ~5 queries totales agrupadas.
    """
    cards = (
        await db.scalars(
            select(DeckCard).where(DeckCard.deck_id == deck.id).order_by(DeckCard.role, DeckCard.name)
        )
    ).all()
    cards_list = list(cards)

    if not cards_list:
        val = deck_validation.validate_deck(deck.format, [])
        return DeckView(
            id=deck.id, name=deck.name, moxfield_id=deck.moxfield_id,
            source_url=deck.source_url, format=deck.format,
            commander_scryfall_id=deck.commander_scryfall_id,
            imported_at=deck.imported_at, updated_at=deck.updated_at,
            cards=[],
            validation=DeckValidation(
                format=val.format, expected=val.expected, counted=val.counted,
                is_valid=val.is_valid, message=val.message, level=val.level,
                breakdown=val.breakdown,
            ),
        )

    # --- BATCH 1: printings de las cartas del deck ---
    scryfall_ids = {c.scryfall_id for c in cards_list}
    printings_by_id = await _printings_by_id(db, scryfall_ids)

    # --- BATCH 2: custom arts frontales (para thumbnails) ---
    custom_ids = {c.custom_art_front_id for c in cards_list if c.custom_art_front_id}
    customs_by_id = await _customs_by_id(db, custom_ids)

    # --- BATCH 3: nº total de impresiones (Scryfall) por oracle_id ---
    oracle_ids = {c.oracle_id for c in cards_list if c.oracle_id}
    prints_count_by_oracle = await _prints_count_by_oracle(db, oracle_ids)

    # --- BATCH 4: nº de custom arts disponibles (por card_name normalizado) ---
    from mpc_forge.services.custom_art import normalize_card_name
    name_norms = {normalize_card_name(c.name) for c in cards_list}
    custom_count_by_name = await _custom_count_by_name(db, name_norms)

    # --- BATCH 5: historial de impresiones agregado (una sola llamada) ---
    stats_map = await history.stats_for_oracle_ids(db, list(oracle_ids))

    # --- Composición sin más queries ---
    import json as _json
    card_views: list[DeckCardView] = []
    for dc in cards_list:
        printing = printings_by_id.get(dc.scryfall_id)
        thumb: str | None = None
        if dc.custom_art_front_id and dc.custom_art_front_id in customs_by_id:
            thumb = custom_art.custom_art_url(customs_by_id[dc.custom_art_front_id].relative_path)
        if not thumb:
            thumb = printing.image_normal if printing else None
        is_dfc = printing.layout in _DFC_LAYOUTS if printing else False
        stat = stats_map.get(dc.oracle_id) if dc.oracle_id else None

        # Reverso: si es DFC, usamos back_image_normal del printing.
        back_thumb: str | None = None
        back_name: str | None = None
        if printing and is_dfc:
            back_thumb = printing.back_image_normal
            back_name = printing.back_name

        # Cartas relacionadas (tokens + meld_result + meld_part).
        related_parts: list[dict[str, str]] = []
        if printing and printing.related_parts:
            try:
                related_parts = _json.loads(printing.related_parts)
            except (ValueError, TypeError):
                related_parts = []

        card_views.append(DeckCardView(
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
            printings_available=prints_count_by_oracle.get(dc.oracle_id, 0) if dc.oracle_id else 0,
            custom_arts_available=custom_count_by_name.get(normalize_card_name(dc.name), 0),
            history_copies=stat.total_copies if stat else 0,
            history_decks=stat.decks if stat else [],
            mana_cost=printing.mana_cost if printing else "",
            cmc=printing.cmc if printing else 0.0,
            type_line=printing.type_line if printing else "",
            colors=printing.colors.split(",") if printing and printing.colors else [],
            color_identity=printing.color_identity.split(",") if printing and printing.color_identity else [],
            rarity=printing.rarity if printing else "",
            keywords=[k for k in (printing.keywords or "").split(",") if k] if printing else [],
            back_thumbnail_url=back_thumb,
            back_name=back_name,
            related_parts=related_parts,
        ))

    # Legalidad: se resuelve con los printings que ya tenemos en memoria del
    # BATCH 1, así que no cuesta ni una query extra.
    illegal = deck_validation.check_legalities(
        deck.format,
        [
            (
                c.name,
                c.role,
                getattr(printings_by_id.get(c.scryfall_id), "legalities", "") or "",
                c.include,
            )
            for c in cards_list
        ],
    )
    val = deck_validation.validate_deck(
        deck.format,
        [(c.role, c.quantity, c.include) for c in cards_list],
        illegal,
    )
    price = _deck_price(cards_list, printings_by_id)
    return DeckView(
        id=deck.id,
        name=deck.name,
        moxfield_id=deck.moxfield_id,
        source_url=deck.source_url,
        format=deck.format,
        commander_scryfall_id=deck.commander_scryfall_id,
        imported_at=deck.imported_at,
        updated_at=deck.updated_at,
        cards=card_views,
        validation=DeckValidation(
            format=val.format,
            expected=val.expected,
            counted=val.counted,
            is_valid=val.is_valid,
            message=val.message,
            level=val.level,
            breakdown=val.breakdown,
            illegal=[
                IllegalCardView(name=c.name, status=c.status, role=c.role)
                for c in val.illegal
            ],
        ),
        price=price,
    )


def _deck_price(
    cards: list[DeckCard], printings_by_id: dict[str, PrintingCache]
) -> DeckPriceView:
    """Suma el precio de mercado de las cartas del mazo.

    Solo cuenta lo que se juega de verdad (mainboard, commander, companion,
    sideboard): el maybeboard es una lista de ideas y meterlo inflaría el
    total sin que el usuario entienda por qué.

    Las cartas sin precio se cuentan aparte en vez de tratarse como 0, para
    que la interfaz pueda decir "al menos X €" y no dar una cifra falsamente
    precisa.
    """
    total_eur = 0.0
    total_usd = 0.0
    priced = 0
    unpriced = 0
    for card in cards:
        if not card.include or card.role not in _PRICED_ROLES:
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


# Roles que cuentan para el precio del mazo (ver `_deck_price`).
_PRICED_ROLES = {"commander", "mainboard", "companion", "sideboard"}


# ---- Recomendador de artes por artista canónico (Fase 3 · T11) --------------
