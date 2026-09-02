"""Endpoints REST para gestión de mazos."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
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
    ImportFromUrlRequest,
    ImportResult,
    SupportedSite,
    UnresolvedEntry,
    UpdateCardRequest,
    UpdateDeckRequest,
)
from mpc_forge.services import custom_art, deck_activity, deck_service, deck_validation, history, preloader
from mpc_forge.services.deck_activity import DeckActivityKind as K

router = APIRouter(prefix="/api/decks", tags=["decks"])
log = logging.getLogger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def _get_moxfield(request: Request) -> MoxfieldClient:
    return request.app.state.moxfield


# --- Import / CRUD -------------------------------------------------------

@router.post("/import/moxfield", response_model=ImportResult)
async def import_moxfield(
    payload: ImportFromMoxfieldRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    moxfield: Annotated[MoxfieldClient, Depends(_get_moxfield)],
) -> ImportResult:
    try:
        deck, unresolved = await deck_service.import_from_moxfield(
            db, scryfall, moxfield, payload.url_or_id,
            include_extras=payload.include_extras,
        )
    except MoxfieldError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    # Registro del import en el timeline del propio mazo — el usuario lo verá
    # como primer evento cuando abra su historial.
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "moxfield",
            "source_ref": payload.url_or_id,
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )


@router.post("/import/text", response_model=ImportResult)
async def import_text(
    payload: ImportFromTextRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> ImportResult:
    deck, unresolved = await deck_service.import_from_plaintext(
        db, scryfall, payload.name, payload.text, payload.format,
        include_extras=payload.include_extras,
    )
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "text",
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )


@router.get("/import/supported-sites", response_model=list[SupportedSite])
async def supported_import_sites() -> list[SupportedSite]:
    """Devuelve la lista de sitios que la app sabe importar por URL.

    Usado por el frontend para pintar los ejemplos y validar en cliente que
    el usuario ha pegado una URL de un sitio soportado.
    """
    from mpc_forge.clients.import_sites import list_supported_sites
    return [SupportedSite(**s) for s in list_supported_sites()]


@router.post("/import/url", response_model=ImportResult)
async def import_url(
    payload: ImportFromUrlRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> ImportResult:
    """Import unificado por URL. El sitio se detecta automáticamente.

    Errores:
      - 400 si el hostname no matchea ningún sitio soportado.
      - 502 si el sitio responde mal (mazo privado, timeout, HTML inesperado…).
    """
    from mpc_forge.clients.import_sites.base import ImportSiteError

    try:
        deck, unresolved = await deck_service.import_from_url(
            db, scryfall, payload.url,
            name=payload.name, fmt=payload.format,
            include_extras=payload.include_extras,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except ImportSiteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "url",
            "source_ref": payload.url,
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )


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
    val = deck_validation.validate_deck(deck.format, list(rows))
    return DeckValidation(
        format=val.format,
        expected=val.expected,
        counted=val.counted,
        is_valid=val.is_valid,
        message=val.message,
        level=val.level,
        breakdown=val.breakdown,
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
# BÚSQUEDA GLOBAL DE CARTAS ATRAVESANDO TODOS LOS MAZOS
# ================================================================
# Permite responder "¿en qué mazos tengo Sol Ring?" sin abrir uno a uno.
# Case-insensitive, matching por substring, orden por relevancia.

class CardInDeck(BaseModel):
    """Una instancia concreta de una carta dentro de un mazo del usuario."""
    deck_card_id: int
    deck_id: int
    deck_name: str
    deck_format: str
    role: str
    quantity: int
    scryfall_id: str
    printing_set: str | None
    printing_number: str | None
    printing_lang: str | None
    has_custom_art: bool
    include: bool
    thumbnail: str | None  # image_normal del printing (para preview)


class CardSearchGroup(BaseModel):
    """Agrupación por oracle_id — todas las copias de "la misma carta"
    en todos los mazos, incluyendo distintas impresiones."""
    oracle_id: str
    canonical_name: str  # nombre normalizado, tomado del primer resultado
    total_copies: int    # suma de quantities across all decks
    deck_count: int      # nº de mazos distintos donde aparece
    instances: list[CardInDeck]


class CardSearchResponse(BaseModel):
    query: str
    total_groups: int    # nº de cartas distintas que matchean
    total_instances: int # nº total de entradas DeckCard que matchean
    groups: list[CardSearchGroup]


@router.get("/_/search-cards", response_model=CardSearchResponse)
async def search_cards_across_decks(
    q: str,
    db: DbDep,
    limit: int = 200,
) -> CardSearchResponse:
    """Busca cartas por nombre (substring, case-insensitive) en TODOS los mazos.

    Devuelve resultados agrupados por ``oracle_id`` para que "Sol Ring" salga
    una sola vez con todas las instancias que hay en distintos mazos
    (posiblemente con impresiones distintas). Incluye la impresión concreta
    elegida en cada instancia para que el frontend pueda mostrar la thumbnail.

    - Sin resultados si ``q`` tiene menos de 2 caracteres útiles.
    - Los tokens y meld_result se INCLUYEN — a veces quieres saber en qué
      mazos tienes generado un token concreto.
    """
    q_clean = (q or "").strip()
    if len(q_clean) < 2:
        return CardSearchResponse(query=q_clean, total_groups=0, total_instances=0, groups=[])

    # Un LIKE con % delante y detrás. Case-insensitive por defecto en SQLite
    # cuando el patrón LIKE usa ASCII (que es nuestro caso salvo diacríticos
    # exóticos; los nombres de cartas MTG en inglés son puro ASCII).
    pattern = f"%{q_clean}%"
    limit = max(1, min(limit, 500))

    # Traemos DeckCard + Deck + PrintingCache en una sola query con joins.
    rows = (
        await db.execute(
            select(DeckCard, Deck, PrintingCache)
            .join(Deck, Deck.id == DeckCard.deck_id)
            .join(PrintingCache, PrintingCache.scryfall_id == DeckCard.scryfall_id, isouter=True)
            .where(DeckCard.name.ilike(pattern))
            .order_by(DeckCard.name, Deck.updated_at.desc())
            .limit(limit)
        )
    ).all()

    # Agrupamos por oracle_id (o por nombre si no hay oracle_id, poco común).
    groups_map: dict[str, CardSearchGroup] = {}
    for dc, deck, printing in rows:
        key = dc.oracle_id or f"name:{dc.name.lower()}"
        instance = CardInDeck(
            deck_card_id=dc.id,
            deck_id=deck.id,
            deck_name=deck.name,
            deck_format=deck.format,
            role=dc.role,
            quantity=dc.quantity,
            scryfall_id=dc.scryfall_id,
            printing_set=printing.set_code if printing else None,
            printing_number=printing.collector_number if printing else None,
            printing_lang=printing.lang if printing else None,
            has_custom_art=bool(dc.custom_art_front_id),
            include=dc.include,
            thumbnail=printing.image_normal if printing else None,
        )
        if key not in groups_map:
            groups_map[key] = CardSearchGroup(
                oracle_id=dc.oracle_id or "",
                canonical_name=dc.name,  # el primer nombre visto; suele ser consistente
                total_copies=0,
                deck_count=0,
                instances=[],
            )
        groups_map[key].instances.append(instance)
        groups_map[key].total_copies += dc.quantity

    # Recalculamos deck_count (mazos distintos por grupo) y ordenamos.
    for group in groups_map.values():
        group.deck_count = len({inst.deck_id for inst in group.instances})

    # Orden por relevancia: primero las que matchean el inicio del nombre,
    # luego alfabético. "sol" → "Sol Ring" antes de "Consol...".
    q_lower = q_clean.lower()
    def _sort_key(g: CardSearchGroup) -> tuple[int, str]:
        starts = 0 if g.canonical_name.lower().startswith(q_lower) else 1
        return (starts, g.canonical_name.lower())

    ordered = sorted(groups_map.values(), key=_sort_key)

    return CardSearchResponse(
        query=q_clean,
        total_groups=len(ordered),
        total_instances=sum(len(g.instances) for g in ordered),
        groups=ordered,
    )


# ================================================================
# TIMELINE DE ACTIVIDAD DEL MAZO
# ================================================================
# El frontend de /history usa estos endpoints para pintar:
# - El grid de mazos con arte del commander (list_decks_with_activity)
# - El timeline del modal al hacer clic en una card (list_activity)

class ActivityEntry(BaseModel):
    """Evento del timeline serializado.

    ``payload`` viene YA como dict (parseado desde el JSON almacenado) para que
    el frontend no tenga que hacer JSON.parse en cada fila. Si el JSON está
    corrupto por lo que sea, devolvemos ``{}`` en lugar de romper el endpoint.
    """
    id: int
    deck_id: int | None
    deck_name_snapshot: str
    created_at: datetime
    kind: str
    card_name: str | None
    card_scryfall_id: str | None
    card_oracle_id: str | None
    payload: dict
    summary: str


class DeckWithActivityView(BaseModel):
    """Card de mazo para el grid de la vista de historial.

    Trae lo mínimo para pintar la card: arte del commander (o de la primera
    carta si el mazo no tiene commander), nombre, contadores de actividad y
    resumen del último evento.
    """
    id: int
    name: str
    format: str
    imported_at: datetime
    updated_at: datetime
    card_count: int
    activity_count: int
    last_activity_at: datetime | None
    last_activity_kind: str | None
    last_activity_summary: str | None
    commander_scryfall_id: str | None
    # Arte para la card: usamos el image_normal del printing del commander.
    # Si no hay commander, ``None`` y el frontend pinta un placeholder.
    commander_name: str | None
    commander_image_url: str | None


@router.get("/_/with-activity", response_model=list[DeckWithActivityView])
async def list_decks_with_activity(db: DbDep) -> list[DeckWithActivityView]:
    """Lista los mazos + metadata para pintar el grid de la vista de historial.

    OPTIMIZACIÓN: todo en 4 queries fijas independientemente de nº de mazos:
      1. Mazos + card_count (JOIN + GROUP BY)
      2. Contadores de actividad por deck_id (GROUP BY)
      3. Último evento por deck_id (MAX + JOIN, evita N+1)
      4. Printings de commander en batch (WHERE IN, evita N+1)
    """
    from mpc_forge.models import DeckActivity as _DA

    # --- BATCH 1: mazos + count de cartas ---
    deck_rows = (
        await db.execute(
            select(Deck, func.count(DeckCard.id).label("card_count"))
            .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
            .group_by(Deck.id)
            .order_by(Deck.updated_at.desc())
        )
    ).all()

    if not deck_rows:
        return []

    # --- BATCH 2: contadores de actividad por deck_id ---
    activity_counts: dict[int, int] = dict(
        (await db.execute(
            select(_DA.deck_id, func.count(_DA.id))
            .where(_DA.deck_id.isnot(None))
            .group_by(_DA.deck_id)
        )).all()
    )

    # --- BATCH 3: último evento por mazo ---
    # Con SQLite la forma más portable sin CTE es una subquery correlacionada.
    # Usamos MAX(id) porque los ids son autoincremental → correlaciona con
    # created_at DESC. Un solo query en vez de N (era el N+1 anterior).
    last_id_subq = (
        select(func.max(_DA.id).label("last_id"), _DA.deck_id.label("d"))
        .where(_DA.deck_id.isnot(None))
        .group_by(_DA.deck_id)
        .subquery()
    )
    last_rows = (
        await db.execute(
            select(_DA.deck_id, _DA.created_at, _DA.kind, _DA.summary)
            .join(last_id_subq, _DA.id == last_id_subq.c.last_id)
        )
    ).all()
    last_activity: dict[int, tuple[datetime, str, str]] = {
        deck_id: (created_at, kind, summary)
        for deck_id, created_at, kind, summary in last_rows
    }

    # --- BATCH 4: printings de commanders en una única query ---
    commander_ids = {d.commander_scryfall_id for d, _ in deck_rows if d.commander_scryfall_id}
    printings_by_id: dict[str, PrintingCache] = {}
    if commander_ids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(commander_ids))
            )
        ).all()
        printings_by_id = {p.scryfall_id: p for p in rows}

    # --- Composición sin más queries ---
    out: list[DeckWithActivityView] = []
    for deck, card_count in deck_rows:
        commander_name: str | None = None
        commander_image: str | None = None
        if deck.commander_scryfall_id:
            p = printings_by_id.get(deck.commander_scryfall_id)
            if p:
                commander_name = p.name
                commander_image = p.image_normal or p.image_large

        last = last_activity.get(deck.id)
        out.append(DeckWithActivityView(
            id=deck.id,
            name=deck.name,
            format=deck.format,
            imported_at=deck.imported_at,
            updated_at=deck.updated_at,
            card_count=int(card_count or 0),
            activity_count=int(activity_counts.get(deck.id, 0)),
            last_activity_at=last[0] if last else None,
            last_activity_kind=last[1] if last else None,
            last_activity_summary=last[2] if last else None,
            commander_scryfall_id=deck.commander_scryfall_id,
            commander_name=commander_name,
            commander_image_url=commander_image,
        ))
    return out


@router.get("/{deck_id}/activity", response_model=list[ActivityEntry])
async def list_activity(
    deck_id: int,
    db: DbDep,
    kinds: str | None = None,   # csv: "card_added,card_moved"
    limit: int = 500,
) -> list[ActivityEntry]:
    """Devuelve las últimas ``limit`` entradas del timeline de un mazo.

    Filtro opcional por ``kinds`` (csv). Si el mazo no existe, 404.
    """
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    limit = max(1, min(limit, 2000))  # bound duro para evitar payloads absurdos
    rows = await deck_activity.list_for_deck(db, deck_id, kinds=kinds_list, limit=limit)

    def _parse_payload(raw: str) -> dict:
        # payload_json puede estar corrupto en teoría (edición manual de la BD,
        # migración fallida…). No queremos romper la vista por eso.
        import json as _json
        try:
            v = _json.loads(raw or "{}")
            return v if isinstance(v, dict) else {"_raw": v}
        except (ValueError, TypeError):
            return {"_error": "invalid_json", "_raw": raw}

    return [
        ActivityEntry(
            id=r.id,
            deck_id=r.deck_id,
            deck_name_snapshot=r.deck_name_snapshot,
            created_at=r.created_at,
            kind=r.kind,
            card_name=r.card_name,
            card_scryfall_id=r.card_scryfall_id,
            card_oracle_id=r.card_oracle_id,
            payload=_parse_payload(r.payload_json),
            summary=r.summary,
        )
        for r in rows
    ]


class UndoResponse(BaseModel):
    ok: bool
    summary: str
    deck_id: int


@router.post("/{deck_id}/activity/{event_id}/undo", response_model=UndoResponse)
async def undo_event_endpoint(deck_id: int, event_id: int, db: DbDep) -> UndoResponse:
    """Deshace un evento del timeline aplicando su operación inversa.

    Solo funciona para eventos reversibles (ver ``services.undo.UNDOABLE_KINDS``)
    y solo si el estado actual del mazo permite la reversión con seguridad
    (no puedes deshacer un movimiento si el usuario ha movido la carta otra vez
    en medio — devuelve 409 con la razón).

    Devuelve 400 si el evento no admite undo por su tipo, 404 si no existe,
    409 si existe pero el estado ha divergido.
    """
    from mpc_forge.models import DeckActivity as _DA
    from mpc_forge.services import undo as undo_svc

    event = await db.get(_DA, event_id)
    if not event or event.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evento no encontrado")

    ok, reason = await undo_svc.can_undo(event)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, reason)

    try:
        result = await undo_svc.undo_event(db, event)
    except undo_svc.UndoNotSupported as e:
        # Estado en BD no permite el undo (409 Conflict es semánticamente correcto)
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))

    return UndoResponse(ok=True, summary=result["summary"], deck_id=deck_id)


@router.get("/_/undoable-kinds")
async def get_undoable_kinds(response: Response) -> list[str]:
    """Lista de kinds que admiten undo. El frontend la usa para decidir qué
    eventos muestran el botón "Deshacer" en el timeline.

    Cache HTTP: es una constante literal, solo cambia con deploy nuevo.
    1 hora balancea "no re-fetchar" y "que pille cambios sin borrar caché".
    """
    from mpc_forge.services import undo as undo_svc
    response.headers["Cache-Control"] = "public, max-age=3600"
    return sorted(undo_svc.UNDOABLE_KINDS)


# ================================================================
# ANÁLISIS DE TOKENS DEL MAZO
# ================================================================

@router.get("/{deck_id}/tokens-analysis")
async def tokens_analysis(
    deck_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> dict:
    """Devuelve todos los tokens únicos que las cartas del mazo generan.

    Consolida los `related_parts` (filtrando SOLO `component=='token'`) de
    las cartas activas del mazo (con include=True), dedupe por scryfall_id,
    y para cada token indica:
      - metadata (nombre, tipo, colores, imagen)
      - qué carta(s) del mazo lo genera
      - si ya está en el mazo (con role='tokens')

    Los meld parts/results NO se incluyen aquí: van por el botón individual
    de cada carta porque son específicos y no consolidables.
    """
    import json as _json

    # Cartas activas del mazo (excluyendo tokens/meld_result — los generadores
    # son commander/mainboard/sideboard, no queremos que los tokens generen tokens
    # de sí mismos si por accidente tuvieran related_parts).
    generator_roles = {"commander", "mainboard", "sideboard"}
    cards = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck_id,
                DeckCard.include.is_(True),
                DeckCard.role.in_(generator_roles),
            )
        )
    ).all()

    if not cards:
        return {"tokens": [], "total_unique": 0, "already_in_deck": 0, "missing": 0}

    # BATCH: printings de todos los generadores (para leer related_parts)
    scryfall_ids = {c.scryfall_id for c in cards}
    printings = (
        await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids))
        )
    ).all()
    printings_by_id = {p.scryfall_id: p for p in printings}

    # Recolectar tokens únicos y quién los genera
    # tokens_map[scryfall_id_del_token] = {
    #   "name": str, "generated_by": [{deck_card_id, name, quantity}, ...]
    # }
    tokens_map: dict[str, dict] = {}
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        if not printing or not printing.related_parts:
            continue
        try:
            related = _json.loads(printing.related_parts)
        except (ValueError, TypeError):
            continue
        for part in related:
            if part.get("component") != "token":
                continue
            token_sfid = part.get("id")
            if not token_sfid:
                continue
            entry = tokens_map.setdefault(token_sfid, {
                "name": part.get("name") or "Token",
                "generated_by": [],
            })
            entry["generated_by"].append({
                "deck_card_id": dc.id,
                "name": dc.name,
                "quantity": dc.quantity,
            })

    if not tokens_map:
        return {"tokens": [], "total_unique": 0, "already_in_deck": 0, "missing": 0}

    # BATCH: metadata de todos los tokens desde cache local (imagen, tipo, etc.)
    token_sfids = list(tokens_map.keys())
    token_printings = (
        await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(token_sfids))
        )
    ).all()
    token_meta_by_id = {p.scryfall_id: p for p in token_printings}

    # BATCH: qué tokens ya están en el mazo (por scryfall_id)
    already_in_deck_rows = (
        await db.execute(
            select(DeckCard.id, DeckCard.scryfall_id, DeckCard.quantity)
            .where(
                DeckCard.deck_id == deck_id,
                DeckCard.scryfall_id.in_(token_sfids),
            )
        )
    ).all()
    in_deck_by_sfid = {sfid: (dc_id, qty) for dc_id, sfid, qty in already_in_deck_rows}

    # Para tokens sin metadata cacheada, la pedimos a Scryfall (uno por uno con
    # el rate limit de ScryfallClient). Suele ser rápido porque son pocos por mazo.
    missing_meta = [s for s in token_sfids if s not in token_meta_by_id]
    for sfid in missing_meta:
        try:
            raw = await scryfall.by_id(sfid)
            if raw:
                cached = await deck_service.upsert_printing(db, raw)
                token_meta_by_id[sfid] = cached
        except Exception as e:  # noqa: BLE001
            log.warning("No se pudo cachear metadata de token %s: %s", sfid, e)
    if missing_meta:
        await db.commit()

    # Ensamblar respuesta
    tokens_out = []
    already_count = 0
    for sfid, info in tokens_map.items():
        meta = token_meta_by_id.get(sfid)
        deck_card_id, qty_in_deck = in_deck_by_sfid.get(sfid, (None, 0))
        in_deck = deck_card_id is not None
        if in_deck:
            already_count += 1
        tokens_out.append({
            "scryfall_id": sfid,
            "name": (meta.name if meta else info["name"]) or "Token",
            "type_line": meta.type_line if meta else "",
            "colors": meta.colors.split(",") if (meta and meta.colors) else [],
            "image_url": meta.image_normal if meta else None,
            "set_code": meta.set_code if meta else "",
            "in_deck": in_deck,
            "deck_card_id": deck_card_id,
            "quantity_in_deck": qty_in_deck,
            "generated_by": info["generated_by"],
        })

    # Orden estable: primero los que faltan, luego los que están, alfabético por nombre
    tokens_out.sort(key=lambda t: (t["in_deck"], t["name"].lower()))

    return {
        "tokens": tokens_out,
        "total_unique": len(tokens_out),
        "already_in_deck": already_count,
        "missing": len(tokens_out) - already_count,
    }


class TokensAddManyRequest(BaseModel):
    scryfall_ids: list[str]


@router.post("/{deck_id}/tokens-add-many", response_model=list[DeckCardView])
async def tokens_add_many(
    deck_id: int,
    payload: TokensAddManyRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[DeckCardView]:
    """Añade varios tokens al mazo de una vez con role='tokens'.

    - Idempotente: si un token ya está, lo salta (no incrementa quantity).
    - Los tokens NO cuentan para el mazo de 100 (role='tokens' está excluido
      de _COUNTING_ROLES_BY_FORMAT) pero SÍ van al PDF/XML (include=True).
    """
    if not payload.scryfall_ids:
        return []

    # ¿Qué scryfall_ids ya están?
    existing_ids = {
        r for r in (
            await db.scalars(
                select(DeckCard.scryfall_id).where(DeckCard.deck_id == deck_id)
            )
        ).all()
    }

    # --- OPTIMIZACIÓN: batch prefetch de printings ya cacheados ---
    # Antes: db.get(PrintingCache, sfid) por cada id (N queries).
    # Ahora: 1 query WHERE IN, luego solo los que falten los pedimos a Scryfall.
    ids_to_check = [s for s in payload.scryfall_ids if s not in existing_ids]
    cached_printings: dict[str, PrintingCache] = {}
    if ids_to_check:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(ids_to_check))
            )
        ).all()
        cached_printings = {p.scryfall_id: p for p in rows}

    added: list[DeckCard] = []
    for sfid in payload.scryfall_ids:
        if sfid in existing_ids:
            continue
        cached = cached_printings.get(sfid)
        if not cached:
            raw = await scryfall.by_id(sfid)
            if not raw:
                continue
            cached = await deck_service.upsert_printing(db, raw)
        new_dc = DeckCard(
            deck_id=deck_id,
            oracle_id=cached.oracle_id or "",
            name=cached.name or "Token",
            quantity=1,
            scryfall_id=sfid,
            role="tokens",  # excluido del count del mazo, incluido en PDF/XML
            include=True,
        )
        db.add(new_dc)
        added.append(new_dc)
        existing_ids.add(sfid)

    if added:
        await deck_activity.log_event(
            db, deck_id, K.RELATED_ADDED,
            payload={
                "count": len(added),
                "kind": "tokens",
                "card_names": [dc.name for dc in added][:20],
            },
        )
    await db.commit()
    for dc in added:
        await db.refresh(dc)
    return await _deckcards_to_views(db, added)


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

    # --- OPTIMIZACIÓN: batch prefetch de printings ---
    # Antes: db.get(PrintingCache, sfid) por cada part (N queries en el bucle).
    # Ahora: 1 query WHERE IN por adelantado.
    candidate_sfids = {
        part.get("id") for part in related
        if part.get("id") and part["id"] not in existing_ids
    }
    cached_map: dict[str, PrintingCache] = {}
    if candidate_sfids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(candidate_sfids))
            )
        ).all()
        cached_map = {p.scryfall_id: p for p in rows}

    added: list[DeckCard] = []
    for part in related:
        sfid = part.get("id")
        if not sfid or sfid in existing_ids:
            continue
        # Asegurar que el printing está cacheado
        cached = cached_map.get(sfid)
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

    if added:
        # Contamos por componente para el summary (tokens vs meld_result…)
        components: dict[str, int] = {}
        for part in related:
            if part.get("id") in {a.scryfall_id for a in added}:
                components[part.get("component") or "related"] = \
                    components.get(part.get("component") or "related", 0) + 1
        await deck_activity.log_event(
            db, deck_id, K.RELATED_ADDED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload={
                "count": len(added),
                "kind": "related",
                "components": components,
                "trigger_card": dc.name,
                "card_names": [a.name for a in added][:20],
            },
        )
    await db.commit()
    for dc in added:
        await db.refresh(dc)
    return await _deckcards_to_views(db, added)


# ================================================================
# LOCALIZACIÓN DE ARTE (idioma de las cartas)
# ================================================================

# Idiomas soportados por Scryfall que exponemos en la UI. La lista completa
# es más larga (he, la, grc, ar, sa, ph, qya…) pero solo tienen impresiones
# reales unas pocas: mantenemos las principales para no abrumar al usuario.
SUPPORTED_LANGS: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "zhs": "简体中文",
    "zht": "繁體中文",
}


class LocalizeDeckRequest(BaseModel):
    lang: str  # Uno de los códigos de SUPPORTED_LANGS


class LocalizeDeckResponse(BaseModel):
    lang: str
    localized: int          # nº de cartas cuyo scryfall_id se cambió al localizado
    unchanged: int          # nº que ya estaban en ese idioma
    unavailable: list[str]  # nombres de cartas sin impresión en ese idioma
    skipped_custom: int     # nº saltadas por tener custom art frontal


@router.get("/_/supported-langs")
async def get_supported_langs(response: Response) -> dict[str, str]:
    """Diccionario code → label para poblar el selector de idiomas del frontend.

    Cache HTTP: contenido esencialmente constante. 1 hora es suficiente para
    que el navegador no pida esto en cada carga del deck editor.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return SUPPORTED_LANGS


@router.post("/{deck_id}/localize", response_model=LocalizeDeckResponse)
async def localize_deck_endpoint(
    deck_id: int,
    payload: LocalizeDeckRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> LocalizeDeckResponse:
    """Cambia todas las cartas del mazo al idioma pedido, cuando exista impresión.

    - Cartas con custom_art_front_id se saltan (respeta el arte custom del usuario).
    - Cartas ya en ese idioma no se tocan.
    - Cartas sin impresión disponible en ese idioma conservan la impresión actual
      y se listan en ``unavailable`` para que el usuario sepa cuáles siguen en su
      idioma original.

    Los printings localizados se cachean como filas independientes de
    ``PrintingCache`` (Scryfall les da su propio scryfall_id por idioma), por lo
    que llamar dos veces con el mismo idioma es prácticamente gratis la segunda
    vez.
    """
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    if payload.lang not in SUPPORTED_LANGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Idioma no soportado: {payload.lang!r}. Válidos: {sorted(SUPPORTED_LANGS)}",
        )

    result = await deck_service.localize_deck(db, scryfall, deck_id, payload.lang)
    # Solo dejamos huella si algo cambió realmente (o hubo cartas no disponibles
    # que el usuario debería conocer). Si todo está ya en ese idioma y no hay
    # unavailables, no ensuciamos el timeline.
    if result["localized"] > 0 or result["unavailable"]:
        await deck_activity.log_event(
            db, deck_id, K.DECK_LOCALIZED,
            payload={
                "lang": payload.lang,
                "localized": result["localized"],
                "unchanged": result["unchanged"],
                "unavailable": result["unavailable"],
                "skipped_custom": result["skipped_custom"],
            },
            deck_name=deck.name,
        )
        await db.commit()
    return LocalizeDeckResponse(**result)


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
                image_small=custom_art.custom_art_url(ca.relative_path),
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
            rarity=p.rarity,
            face="front",
            image_small=p.image_normal,
            # Solo se marca como chosen si no hay custom front seleccionado.
            is_chosen=(dc.custom_art_front_id is None and p.scryfall_id == dc.scryfall_id),
            is_preferred=(pref is not None and pref.scryfall_id == p.scryfall_id),
            is_last_used=(last_used is not None and last_used == p.scryfall_id),
        ))
    return options


# ================================================================
# PRECARGA DE PRINTS EN BACKGROUND
# ================================================================
# Cuando el usuario abre un mazo, disparamos precarga de todas las
# impresiones alternativas (fetch_printings_for_oracle) en background.
# Así cuando abre el modal de arte para cualquier carta, ya está cacheado
# y la respuesta es instantánea desde BD (sin llamar a Scryfall).

@router.post("/{deck_id}/preload-prints")
async def preload_prints(
    deck_id: int,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> dict:
    """Arranca precarga en background. Devuelve el estado inicial (total, done=0).

    Si ya hay una precarga en curso para este mazo, la cancela y arranca una nueva.
    Los oracle_ids ya cacheados (>=2 prints en BD) se procesan en <10ms cada uno;
    solo los no cacheados llaman a Scryfall API.
    """
    state = await preloader.start(deck_id, scryfall)
    return state.to_dict()


@router.get("/{deck_id}/preload-progress")
async def preload_progress(deck_id: int) -> dict:
    """Estado de la precarga (para polling desde el frontend)."""
    state = preloader.get_state(deck_id)
    if state is None:
        return {"deck_id": deck_id, "total": 0, "done": 0, "in_progress": False}
    return state.to_dict()


@router.post("/{deck_id}/preload-cancel")
async def preload_cancel(deck_id: int) -> dict:
    """Cancela la precarga en curso (ej. cuando el user cambia de mazo)."""
    await preloader.cancel(deck_id)
    return {"deck_id": deck_id, "cancelled": True}


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

    # Snapshot ANTES de mutar. Guardamos el set/número para el timeline —
    # es lo más útil para el usuario, más que el scryfall_id.
    old_sfid = dc.scryfall_id
    old_custom_front = dc.custom_art_front_id
    old_custom_back = dc.custom_art_back_id
    old_printing = await db.get(PrintingCache, old_sfid) if old_sfid else None
    old_set = old_printing.set_code if old_printing else None
    old_number = old_printing.collector_number if old_printing else None

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

    # Reunimos el nuevo estado para el payload del timeline.
    new_printing = await db.get(PrintingCache, dc.scryfall_id) if dc.scryfall_id else None
    activity_payload = {
        "face": payload.face,
        "kind": "custom" if payload.custom_art_id else "official",
    }
    if payload.custom_art_id:
        ca = await db.get(CustomArt, payload.custom_art_id)
        activity_payload.update({
            "custom_art_id": payload.custom_art_id,
            "custom_filename": ca.filename if ca else None,
            "custom_variant": ca.variant_label if ca else None,
        })
    else:
        activity_payload.update({
            "old_scryfall_id": old_sfid,
            "new_scryfall_id": dc.scryfall_id,
            "old_set": old_set, "old_number": old_number,
            "new_set": new_printing.set_code if new_printing else None,
            "new_number": new_printing.collector_number if new_printing else None,
            "remember_globally": payload.remember_globally,
        })
    # Solo loggeamos si realmente cambió algo (evita ruido si el usuario
    # hace click en el arte que ya estaba seleccionado).
    changed = (
        dc.scryfall_id != old_sfid
        or dc.custom_art_front_id != old_custom_front
        or dc.custom_art_back_id != old_custom_back
    )
    if changed:
        await deck_activity.log_event(
            db, deck_id, K.CARD_ART_CHANGED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload=activity_payload,
        )
    await db.commit()
    return await _deckcard_to_view(db, dc)


@router.post("/{deck_id}/cards/{card_id}/toggle", response_model=DeckCardView)
async def toggle_include(deck_id: int, card_id: int, db: DbDep) -> DeckCardView:
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    dc.include = not dc.include
    await deck_activity.log_event(
        db, deck_id, K.CARD_INCLUDE_TOGGLED,
        card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
        payload={"new_include": dc.include, "role": dc.role, "quantity": dc.quantity},
    )
    await db.commit()
    return await _deckcard_to_view(db, dc)


# --- Helpers de vista -----------------------------------------------------

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
    """Versión single-card (para endpoints que devuelven una sola carta).

    Para vistas completas de mazo, usar `_deck_to_view` que hace batch de todo.
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
