"""Alta, consulta, edición y borrado de mazos y de sus cartas.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import (
    Depends,
    HTTPException,
    status,
)
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import (
    ArtPreference,
    Deck,
    DeckCard,
    PrintingCache,
)
from mpc_forge.schemas import (
    AddCardRequest,
    DeckCardView,
    DeckValidation,
    DeckView,
    IllegalCardView,
    UpdateCardRequest,
    UpdateDeckRequest,
)
from mpc_forge.services import (
    deck_activity,
    deck_service,
    deck_validation,
)
from mpc_forge.services.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


# --- Import / CRUD -------------------------------------------------------

from mpc_forge.routes.decks._common import (
    DbDep,
    _get_scryfall,
    make_router,
)
from mpc_forge.routes.decks._views import (
    _deck_to_view,
    _deckcard_to_view,
)

router = make_router()


class DeckSummaryView(BaseModel):
    """Vista ligera para listings. Muchísimo más rápida que DeckView completo.

    DeckView incluye la lista completa de cartas con sus 20+ campos cada una
    (mana_cost, colors, keywords, image URLs, history_copies, custom_arts_count…)
    lo cual escala mal con muchos mazos. Para la vista de listado no necesitamos
    ese detalle — solo lo básico. Un cliente que necesite el detalle completo
    llama a ``GET /api/decks/{id}``.
    """
    id: int
    name: str
    format: str
    moxfield_id: str | None
    source_url: str | None
    commander_scryfall_id: str | None
    imported_at: datetime
    updated_at: datetime
    card_count: int
    notes: str = ""


@router.get("/", response_model=list[DeckSummaryView])
async def list_decks(db: DbDep) -> list[DeckSummaryView]:
    """Listado de mazos con lo mínimo para pintar cards.

    OPTIMIZACIÓN: usa un JOIN con COUNT en vez de traer todas las cartas
    (selectinload) y luego llamar a _deck_to_view (que hace 5 queries por
    mazo). Para 20 mazos × 30 cartas pasa de ~100 queries a 1 sola.
    Para 100 mazos × 100 cartas pasa de ~500 queries + serializar 10k rows
    a 1 query con 100 filas agregadas.
    """
    rows = (
        await db.execute(
            select(Deck, func.count(DeckCard.id).label("card_count"))
            .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
            .group_by(Deck.id)
            .order_by(Deck.updated_at.desc())
        )
    ).all()
    return [
        DeckSummaryView(
            id=deck.id,
            name=deck.name,
            format=deck.format,
            moxfield_id=deck.moxfield_id,
            source_url=deck.source_url,
            commander_scryfall_id=deck.commander_scryfall_id,
            imported_at=deck.imported_at,
            updated_at=deck.updated_at,
            card_count=int(card_count or 0),
            notes=deck.notes or "",
        )
        for deck, card_count in rows
    ]


@router.get("/{deck_id}", response_model=DeckView)
async def get_deck(deck_id: int, db: DbDep) -> DeckView:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return await _deck_to_view(db, deck)


@router.get("/{deck_id}/validation", response_model=DeckValidation)
async def get_deck_validation(deck_id: int, db: DbDep) -> DeckValidation:
    """Devuelve SOLO la validación del mazo. Endpoint ultra ligero para el
    frontend — usado por cambios que solo afectan a totales (toggle include,
    change qty, mover a otra sección) para no tener que refrescar el mazo
    entero.

    2 queries fijas (get deck + count agregado por rol). Muy rápido incluso
    con mazos grandes.
    """
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    # Traemos solo (role, quantity, include) — no necesitamos las cartas enteras.
    rows = (
        await db.execute(
            select(DeckCard.role, DeckCard.quantity, DeckCard.include)
            .where(DeckCard.deck_id == deck_id)
        )
    ).all()
    # Legalidades: un LEFT JOIN contra el cache de printings. Las cartas sin
    # printing cacheado salen con legalities vacío y `check_legalities` las
    # ignora, que es el comportamiento correcto (no inventar un veredicto).
    legality_rows = (
        await db.execute(
            select(
                DeckCard.name,
                DeckCard.role,
                PrintingCache.legalities,
                DeckCard.include,
            )
            .outerjoin(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id)
            .where(DeckCard.deck_id == deck_id)
        )
    ).all()
    illegal = deck_validation.check_legalities(
        deck.format,
        [(name, role, legalities or "", include)
         for name, role, legalities, include in legality_rows],
    )
    val = deck_validation.validate_deck(deck.format, list(rows), illegal)
    return DeckValidation(
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
    )


@router.delete("/{deck_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deck(deck_id: int, db: DbDep) -> None:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    await db.delete(deck)
    await db.commit()


class DuplicateDeckRequest(BaseModel):
    name: str | None = None  # si es None, se usa "{original} (copia)"


@router.post("/{deck_id}/duplicate", response_model=DeckView, status_code=status.HTTP_201_CREATED)
async def duplicate_deck(
    deck_id: int, payload: DuplicateDeckRequest, db: DbDep,
) -> DeckView:
    """Duplica un mazo con todas sus cartas.

    El mazo nuevo hereda cartas (con su arte custom, roles, cantidades…) pero
    **no** hereda historial, moxfield_id ni source_url — es un mazo nuevo con
    su propia identidad. Se registra un evento ``deck_created`` en el timeline
    con ``source: "duplicated"`` para que el usuario vea de dónde viene.
    """
    src = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not src:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    new_name = (payload.name or f"{src.name} (copia)").strip()
    if not new_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nombre no puede ser vacío")

    new_deck = Deck(
        name=new_name,
        format=src.format,
        commander_scryfall_id=src.commander_scryfall_id,
        notes=src.notes,
        # moxfield_id y source_url NO se copian — el duplicado es una entidad nueva
    )
    db.add(new_deck)
    await db.flush()  # necesitamos el id para las cartas

    total_qty = 0
    for c in src.cards:
        db.add(DeckCard(
            deck_id=new_deck.id,
            oracle_id=c.oracle_id,
            name=c.name,
            quantity=c.quantity,
            scryfall_id=c.scryfall_id,
            custom_art_front_id=c.custom_art_front_id,
            custom_art_back_id=c.custom_art_back_id,
            role=c.role,
            include=c.include,
        ))
        total_qty += c.quantity

    await deck_activity.log_event(
        db, new_deck.id, K.DECK_CREATED,
        payload={
            "source": "duplicated",
            "source_deck_id": src.id,
            "source_deck_name": src.name,
            "card_count": total_qty,
            "unresolved_count": 0,
        },
        deck_name=new_deck.name,
    )
    await db.commit()
    await db.refresh(new_deck, ["cards"])
    return await _deck_to_view(db, new_deck)


@router.patch("/{deck_id}", response_model=DeckView)
async def update_deck(deck_id: int, payload: UpdateDeckRequest, db: DbDep) -> DeckView:
    """Renombra o edita metadatos del mazo."""
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    old_name = deck.name
    if payload.name is not None:
        deck.name = payload.name
    if payload.format is not None:
        deck.format = payload.format
    if payload.notes is not None:
        deck.notes = payload.notes
    # Solo loggeamos rename porque es el único cambio "material" que le puede
    # importar al usuario en el timeline. Cambios de formato/notas rara vez
    # ocurren y no aportan mucho al historial.
    if payload.name is not None and payload.name != old_name:
        await deck_activity.log_event(
            db, deck_id, K.DECK_RENAMED,
            payload={"old_name": old_name, "new_name": payload.name},
            deck_name=payload.name,
        )
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
    await deck_activity.log_event(
        db, deck_id, K.CARD_ADDED,
        card_name=printing.name,
        card_scryfall_id=printing.scryfall_id,
        card_oracle_id=printing.oracle_id or None,
        payload={"quantity": payload.quantity, "role": payload.role,
                 "stacked": bool(existing)},
        deck_name=deck.name,
    )
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
    # Guardamos el estado previo ANTES de mutar, para poder loggear (old → new).
    old_qty = dc.quantity
    old_role = dc.role

    if payload.quantity is not None and payload.quantity != old_qty:
        dc.quantity = payload.quantity
        await deck_activity.log_event(
            db, deck_id, K.CARD_QTY_CHANGED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload={"old_qty": old_qty, "new_qty": payload.quantity, "role": dc.role},
        )
    if payload.role is not None and payload.role != old_role:
        dc.role = payload.role
        await deck_activity.log_event(
            db, deck_id, K.CARD_MOVED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload={"from_role": old_role, "to_role": payload.role},
        )
    await db.commit()
    return await _deckcard_to_view(db, dc)


@router.delete("/{deck_id}/cards/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card(deck_id: int, card_id: int, db: DbDep) -> None:
    """Elimina una carta del mazo."""
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    # Loggeamos ANTES de borrar para conservar los datos de la carta.
    await deck_activity.log_event(
        db, deck_id, K.CARD_REMOVED,
        card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
        payload={"quantity": dc.quantity, "role": dc.role},
    )
    await db.delete(dc)
    await db.commit()


class ClearRoleResponse(BaseModel):
    role: str
    deleted: int


@router.delete("/{deck_id}/role/{role}", response_model=ClearRoleResponse)
async def clear_role(deck_id: int, role: str, db: DbDep) -> ClearRoleResponse:
    """Elimina TODAS las cartas de un rol/sección del mazo (sideboard, tokens,
    maybeboard, etc.). El frontend confirma antes de llamar — este endpoint no
    pregunta, borra directo. Idempotente: si no hay cartas de ese rol, devuelve
    ``deleted=0`` sin error.
    """
    # Validamos que el mazo existe (para dar 404 claro en vez de "deleted=0" silencioso)
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    cards = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck_id,
                DeckCard.role == role,
            )
        )
    ).all()
    # Snapshot para el timeline: hasta 20 nombres (evita payloads gigantes en
    # sideboards enormes). Solo se usa para mostrar en el modal, no es autoritativo.
    card_names = [c.name for c in cards[:20]]
    total_qty = sum(c.quantity for c in cards)
    for dc in cards:
        await db.delete(dc)
    if cards:
        await deck_activity.log_event(
            db, deck_id, K.ROLE_CLEARED,
            payload={
                "role": role,
                "deleted": len(cards),
                "total_qty": total_qty,
                "card_names_sample": card_names,
                "truncated": len(cards) > len(card_names),
            },
            deck_name=deck.name,
        )
    await db.commit()
    return ClearRoleResponse(role=role, deleted=len(cards))


# ================================================================
