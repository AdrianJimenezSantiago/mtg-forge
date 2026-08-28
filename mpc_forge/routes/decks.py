"""Endpoints REST para gestión de mazos."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    ImportResult,
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

    Pensado para pintar cards visuales, no para el editor. Optimizado para
    minimizar queries: usamos joins agregados para no ir carta a carta.
    """
    # Traemos todos los mazos con su count de cartas en una sola query.
    deck_rows = (
        await db.execute(
            select(Deck, func.count(DeckCard.id).label("card_count"))
            .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
            .group_by(Deck.id)
            .order_by(Deck.updated_at.desc())
        )
    ).all()

    # Contadores de actividad por deck_id — una sola query agregada.
    from mpc_forge.models import DeckActivity as _DA
    activity_counts = dict(
        (await db.execute(
            select(_DA.deck_id, func.count(_DA.id))
            .where(_DA.deck_id.isnot(None))
            .group_by(_DA.deck_id)
        )).all()
    )

    # Último evento por mazo. Con SQLite la forma más simple sin CTE es
    # una subquery. Como los mazos suelen ser pocos (<50), lo hacemos con
    # una query por mazo — el índice compuesto (deck_id, created_at DESC)
    # que ya creamos en init_db hace que sea O(log N) por mazo.
    last_activity: dict[int, tuple[datetime, str, str]] = {}
    for deck, _ in deck_rows:
        last = await deck_activity.last_activity_for_deck(db, deck.id)
        if last:
            last_activity[deck.id] = (last.created_at, last.kind, last.summary)

    out: list[DeckWithActivityView] = []
    for deck, card_count in deck_rows:
        # Arte del commander: si hay commander_scryfall_id resolvemos su printing.
        commander_name: str | None = None
        commander_image: str | None = None
        if deck.commander_scryfall_id:
            printing = await db.get(PrintingCache, deck.commander_scryfall_id)
            if printing:
                commander_name = printing.name
                commander_image = printing.image_normal or printing.image_large

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

    added: list[DeckCard] = []
    for sfid in payload.scryfall_ids:
        if sfid in existing_ids:
            continue
        cached = await db.get(PrintingCache, sfid)
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
    return [await _deckcard_to_view(db, dc) for dc in added]


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
    return [await _deckcard_to_view(db, dc) for dc in added]


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
async def get_supported_langs() -> dict[str, str]:
    """Diccionario code → label para poblar el selector de idiomas del frontend."""
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
