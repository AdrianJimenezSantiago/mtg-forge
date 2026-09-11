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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.schemas import DeckCardView, DeckValidation, DeckView
from mpc_forge.services import custom_art, deck_validation, history

log = logging.getLogger(__name__)


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
    printings_by_id: dict[str, PrintingCache] = {
        p.scryfall_id: p for p in (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids))
            )
        ).all()
    }

    # --- BATCH 2: custom arts frontales ---
    custom_ids = {c.custom_art_front_id for c in cards if c.custom_art_front_id}
    customs_by_id: dict[int, CustomArt] = {}
    if custom_ids:
        customs_by_id = {
            ca.id: ca for ca in (
                await db.scalars(select(CustomArt).where(CustomArt.id.in_(custom_ids)))
            ).all()
        }

    # --- BATCH 3: nº total de impresiones por oracle_id ---
    oracle_ids = {c.oracle_id for c in cards if c.oracle_id}
    prints_count_by_oracle: dict[str, int] = {}
    if oracle_ids:
        rows = (
            await db.execute(
                select(PrintingCache.oracle_id, func.count(PrintingCache.scryfall_id))
                .where(PrintingCache.oracle_id.in_(oracle_ids))
                .group_by(PrintingCache.oracle_id)
            )
        ).all()
        prints_count_by_oracle = {oid: int(n) for oid, n in rows}

    # --- BATCH 4: nº de custom arts disponibles por nombre normalizado ---
    name_norms = {custom_art.normalize_card_name(c.name) for c in cards}
    custom_count_by_name: dict[str, int] = {}
    if name_norms:
        rows = (
            await db.execute(
                select(CustomArt.card_name_normalized, func.count(CustomArt.id))
                .where(
                    CustomArt.card_name_normalized.in_(name_norms),
                    CustomArt.face == "front",
                )
                .group_by(CustomArt.card_name_normalized)
            )
        ).all()
        custom_count_by_name = {n: int(c) for n, c in rows}

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
    printings_rows = (
        await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids))
        )
    ).all()
    printings_by_id: dict[str, PrintingCache] = {p.scryfall_id: p for p in printings_rows}

    # --- BATCH 2: custom arts frontales (para thumbnails) ---
    custom_ids = {c.custom_art_front_id for c in cards_list if c.custom_art_front_id}
    customs_by_id: dict[int, CustomArt] = {}
    if custom_ids:
        rows = (
            await db.scalars(select(CustomArt).where(CustomArt.id.in_(custom_ids)))
        ).all()
        customs_by_id = {ca.id: ca for ca in rows}

    # --- BATCH 3: nº total de impresiones (Scryfall) por oracle_id ---
    oracle_ids = {c.oracle_id for c in cards_list if c.oracle_id}
    prints_count_by_oracle: dict[str, int] = {}
    if oracle_ids:
        rows = (
            await db.execute(
                select(PrintingCache.oracle_id, func.count(PrintingCache.scryfall_id))
                .where(PrintingCache.oracle_id.in_(oracle_ids))
                .group_by(PrintingCache.oracle_id)
            )
        ).all()
        prints_count_by_oracle = {oid: int(n) for oid, n in rows}

    # --- BATCH 4: nº de custom arts disponibles (por card_name normalizado) ---
    from mpc_forge.services.custom_art import normalize_card_name
    name_norms = {normalize_card_name(c.name) for c in cards_list}
    custom_count_by_name: dict[str, int] = {}
    if name_norms:
        rows = (
            await db.execute(
                select(CustomArt.card_name_normalized, func.count(CustomArt.id))
                .where(
                    CustomArt.card_name_normalized.in_(name_norms),
                    CustomArt.face == "front",
                )
                .group_by(CustomArt.card_name_normalized)
            )
        ).all()
        custom_count_by_name = {n: int(c) for n, c in rows}

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

    val = deck_validation.validate_deck(
        deck.format, [(c.role, c.quantity, c.include) for c in cards_list]
    )
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
        ),
    )


# ---- Recomendador de artes por artista canónico (Fase 3 · T11) --------------
