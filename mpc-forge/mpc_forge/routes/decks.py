"""Endpoints REST para gestión de mazos."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.clients.moxfield import MoxfieldClient, MoxfieldError
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session
from mpc_forge.models import ArtPreference, CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.schemas import (
    AddCardRequest,
    ArtOption,
    ChangeArtRequest,
    DeckCardView,
    DeckValidation,
    DeckView,
    ImportFromMoxfieldRequest,
    ImportFromTextRequest,
    UpdateCardRequest,
    UpdateDeckRequest,
)
from mpc_forge.services import custom_art, deck_service, deck_validation, history

router = APIRouter(prefix="/api/decks", tags=["decks"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def _get_moxfield(request: Request) -> MoxfieldClient:
    return request.app.state.moxfield


# --- Import / CRUD -------------------------------------------------------

@router.post("/import/moxfield", response_model=DeckView)
async def import_moxfield(
    payload: ImportFromMoxfieldRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    moxfield: Annotated[MoxfieldClient, Depends(_get_moxfield)],
) -> DeckView:
    try:
        deck = await deck_service.import_from_moxfield(
            db, scryfall, moxfield, payload.url_or_id,
            include_extras=payload.include_extras,
        )
    except MoxfieldError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    return await _deck_to_view(db, deck)


@router.post("/import/text", response_model=DeckView)
async def import_text(
    payload: ImportFromTextRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> DeckView:
    deck = await deck_service.import_from_plaintext(
        db, scryfall, payload.name, payload.text, payload.format,
        include_extras=payload.include_extras,
    )
    return await _deck_to_view(db, deck)


@router.get("/", response_model=list[DeckView])
async def list_decks(db: DbDep) -> list[DeckView]:
    decks = (
        await db.scalars(
            select(Deck).options(selectinload(Deck.cards)).order_by(Deck.updated_at.desc())
        )
    ).all()
    return [await _deck_to_view(db, d) for d in decks]


@router.get("/{deck_id}", response_model=DeckView)
async def get_deck(deck_id: int, db: DbDep) -> DeckView:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return await _deck_to_view(db, deck)


@router.delete("/{deck_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deck(deck_id: int, db: DbDep) -> None:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    await db.delete(deck)
    await db.commit()


@router.patch("/{deck_id}", response_model=DeckView)
async def update_deck(deck_id: int, payload: UpdateDeckRequest, db: DbDep) -> DeckView:
    """Renombra o edita metadatos del mazo."""
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    if payload.name is not None:
        deck.name = payload.name
    if payload.format is not None:
        deck.format = payload.format
    if payload.notes is not None:
        deck.notes = payload.notes
    await db.commit()
    return await _deck_to_view(db, deck)


@router.post("/{deck_id}/cards", response_model=DeckCardView, status_code=status.HTTP_201_CREATED)
async def add_card(
    deck_id: int,
    payload: AddCardRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> DeckCardView:
    """Añade una carta al mazo. La resuelve contra Scryfall por nombre (o set+num)."""
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    # Resolver la carta contra Scryfall (usa named si no hay set/num específico)
    if payload.set_code and payload.collector_number:
        raw = await scryfall.by_set_and_number(payload.set_code, payload.collector_number)
    else:
        raw = await scryfall.named(payload.name, set_code=payload.set_code)
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Carta «{payload.name}» no encontrada en Scryfall")

    printing = await deck_service.upsert_printing(db, raw)

    # Si ya existe una entrada con el mismo oracle_id y rol, sumamos cantidad
    existing = None
    if printing.oracle_id:
        stmt = select(DeckCard).where(
            DeckCard.deck_id == deck_id,
            DeckCard.oracle_id == printing.oracle_id,
            DeckCard.role == payload.role,
        )
        existing = (await db.scalars(stmt)).first()

    if existing:
        existing.quantity += payload.quantity
        dc = existing
    else:
        # Aplica preferencia global si existe
        chosen_sfid = printing.scryfall_id
        if printing.oracle_id:
            pref = await db.get(ArtPreference, printing.oracle_id)
            if pref:
                chosen_sfid = pref.scryfall_id
        dc = DeckCard(
            deck_id=deck_id,
            oracle_id=printing.oracle_id or "",
            name=printing.name,
            quantity=payload.quantity,
            scryfall_id=chosen_sfid,
            role=payload.role,
            include=True,
        )
        db.add(dc)
    await db.commit()
    await db.refresh(dc)
    return await _deckcard_to_view(db, dc)


@router.patch("/{deck_id}/cards/{card_id}", response_model=DeckCardView)
async def update_card(
    deck_id: int, card_id: int, payload: UpdateCardRequest, db: DbDep,
) -> DeckCardView:
    """Edita cantidad y/o rol de una carta del mazo."""
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    if payload.quantity is not None:
        dc.quantity = payload.quantity
    if payload.role is not None:
        dc.role = payload.role
    await db.commit()
    return await _deckcard_to_view(db, dc)


@router.delete("/{deck_id}/cards/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card(deck_id: int, card_id: int, db: DbDep) -> None:
    """Elimina una carta del mazo."""
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    await db.delete(dc)
    await db.commit()


@router.post("/{deck_id}/cards/{card_id}/add-related", response_model=list[DeckCardView])
async def add_related_cards(
    deck_id: int,
    card_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[DeckCardView]:
    """Añade automáticamente al mazo las cartas relacionadas (tokens, meld_result, meld_part).

    Se añaden con role="tokens" para que aparezcan en la sección Tokens y no
    cuenten para el mazo de 100. Cada una con quantity=1.
    Se omiten las que ya estén en el mazo.
    """
    import json as _json
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    printing = await db.get(PrintingCache, dc.scryfall_id)
    if not printing or not printing.related_parts:
        return []

    try:
        related = _json.loads(printing.related_parts)
    except (ValueError, TypeError):
        return []

    # ¿Qué scryfall_ids ya tiene el mazo?
    existing_ids = {
        r for r in (
            await db.scalars(select(DeckCard.scryfall_id).where(DeckCard.deck_id == deck_id))
        ).all()
    }

    added: list[DeckCard] = []
    for part in related:
        sfid = part.get("id")
        if not sfid or sfid in existing_ids:
            continue
        # Asegurar que el printing está cacheado
        cached = await db.get(PrintingCache, sfid)
        if not cached:
            raw = await scryfall.by_id(sfid)
            if not raw:
                continue
            cached = await deck_service.upsert_printing(db, raw)

        new_dc = DeckCard(
            deck_id=deck_id,
            oracle_id=cached.oracle_id or "",
            name=cached.name or part.get("name", ""),
            quantity=1,
            scryfall_id=sfid,
            role="tokens",  # se muestra en la sección Tokens y no cuenta para el 100
            include=True,
        )
        db.add(new_dc)
        added.append(new_dc)
        existing_ids.add(sfid)

    await db.commit()
    for dc in added:
        await db.refresh(dc)
    return [await _deckcard_to_view(db, dc) for dc in added]


@router.get("/_/autocomplete")
async def autocomplete_card(
    q: str,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[str]:
    """Autocompleta nombres de cartas usando la API de Scryfall.

    Path bajo /_/ para evitar colisión con los routes de deck_id (int).
    Ante fallos de red o rate limit, devuelve lista vacía (el frontend simplemente
    no muestra sugerencias, no aparece un error molesto).
    """
    if not q or len(q.strip()) < 2:
        return []
    try:
        return await scryfall.autocomplete(q)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Autocomplete falló para %r: %s", q, e)
        return []


# --- Art picker ----------------------------------------------------------

@router.get("/{deck_id}/cards/{card_id}/prints", response_model=list[ArtOption])
async def list_printings_for_card(
    deck_id: int,
    card_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[ArtOption]:
    """Devuelve TODAS las opciones de arte para una carta: custom + Scryfall.

    Los custom aparecen primero. Cada uno lleva `face` para que el frontend sepa
    en qué cara aplicarlo (front / back).
    """
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    options: list[ArtOption] = []

    # 1) Custom arts (frente + reverso, en ese orden)
    for face in ("front", "back"):
        custom_matches = await custom_art.find_for_card(db, dc.name, face=face)
        for ca in custom_matches:
            is_chosen = (
                (face == "front" and dc.custom_art_front_id == ca.id)
                or (face == "back" and dc.custom_art_back_id == ca.id)
            )
            options.append(ArtOption(
                kind="custom",
                custom_art_id=ca.id,
                variant_label=ca.variant_label,
                filename=ca.filename,
                face=face,
                image_small=f"/custom_art/{ca.relative_path}",
                is_chosen=is_chosen,
            ))

    # 2) Impresiones oficiales de Scryfall
    prints = await deck_service.fetch_printings_for_oracle(db, scryfall, dc.oracle_id)
    pref = await db.get(ArtPreference, dc.oracle_id) if dc.oracle_id else None
    last_used = await history.last_scryfall_id_used(db, dc.oracle_id) if dc.oracle_id else None
    for p in prints:
        options.append(ArtOption(
            kind="scryfall",
            scryfall_id=p.scryfall_id,
            set_code=p.set_code,
            set_name=p.set_name,
            collector_number=p.collector_number,
            frame=p.frame,
            border_color=p.border_color,
            full_art=p.full_art,
            textless=p.textless,
            promo=p.promo,
            layout=p.layout,
            artist=p.artist,
            released_at=p.released_at,
            face="front",
            image_small=p.image_normal,
            # Solo se marca como chosen si no hay custom front seleccionado.
            is_chosen=(dc.custom_art_front_id is None and p.scryfall_id == dc.scryfall_id),
            is_preferred=(pref is not None and pref.scryfall_id == p.scryfall_id),
            is_last_used=(last_used is not None and last_used == p.scryfall_id),
        ))
    return options


@router.post("/{deck_id}/cards/change-art", response_model=DeckCardView)
async def change_art(
    deck_id: int,
    payload: ChangeArtRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> DeckCardView:
    """Cambia el arte seleccionado para una carta del mazo.

    - Si `custom_art_id` está poblado: usa ese arte custom en la cara indicada.
    - Si `scryfall_id` está poblado: usa ese arte oficial y limpia el custom
      correspondiente (para front). Optional: recordar globalmente.
    """
    dc = await db.get(DeckCard, payload.deck_card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    if payload.custom_art_id is not None:
        ca = await db.get(CustomArt, payload.custom_art_id)
        if not ca:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "custom_art_id inválido")
        if payload.face == "back":
            dc.custom_art_back_id = ca.id
        else:
            dc.custom_art_front_id = ca.id
    elif payload.scryfall_id is not None:
        # Elección oficial: limpia el custom del frente (o back) y actualiza scryfall_id
        printing = await db.get(PrintingCache, payload.scryfall_id)
        if not printing:
            raw = await scryfall.by_id(payload.scryfall_id)
            if not raw:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "scryfall_id inválido")
            await deck_service.upsert_printing(db, raw)
        if payload.face == "back":
            dc.custom_art_back_id = None
            # El scryfall_id define la carta completa (front+back del DFC), no lo cambiamos aquí
        else:
            dc.custom_art_front_id = None
            dc.scryfall_id = payload.scryfall_id
            if payload.remember_globally and dc.oracle_id:
                pref = await db.get(ArtPreference, dc.oracle_id)
                if pref:
                    pref.scryfall_id = payload.scryfall_id
                else:
                    db.add(ArtPreference(oracle_id=dc.oracle_id, scryfall_id=payload.scryfall_id))
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Debe indicarse scryfall_id o custom_art_id"
        )

    await db.commit()
    return await _deckcard_to_view(db, dc)


@router.post("/{deck_id}/cards/{card_id}/toggle", response_model=DeckCardView)
async def toggle_include(deck_id: int, card_id: int, db: DbDep) -> DeckCardView:
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    dc.include = not dc.include
    await db.commit()
    return await _deckcard_to_view(db, dc)


# --- Helpers de vista -----------------------------------------------------

async def _deckcard_to_view(db: AsyncSession, dc: DeckCard) -> DeckCardView:
    """Versión single-card (para endpoints que devuelven una sola carta).

    Para vistas completas de mazo, usar `_deck_to_view` que hace batch de todo.
    """
    printing = await db.get(PrintingCache, dc.scryfall_id)
    thumb: str | None = None
    if dc.custom_art_front_id:
        ca = await db.get(CustomArt, dc.custom_art_front_id)
        if ca:
            thumb = f"/custom_art/{ca.relative_path}"
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
            thumb = f"/custom_art/{customs_by_id[dc.custom_art_front_id].relative_path}"
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
