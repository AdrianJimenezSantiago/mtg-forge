"""Línea de tiempo por mazo y deshacer de eventos concretos.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import (
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel
from sqlalchemy import func, select

from mpc_forge.models import (
    Deck,
    DeckCard,
    PrintingCache,
)
from mpc_forge.services import (
    deck_activity,
)

log = logging.getLogger(__name__)


# --- Import / CRUD -------------------------------------------------------

from mpc_forge.routes.decks._common import (
    DbDep,
    make_router,
)

router = make_router()


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
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

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
