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
)
from mpc_forge.services import (
    deck_activity,
    deck_covers,
)

log = logging.getLogger(__name__)


from mpc_forge.routes.decks._common import (
    DbDep,
    make_router,
)

router = make_router()


class ActivityEntry(BaseModel):
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
    commander_name: str | None
    commander_image_url: str | None


@router.get("/_/with-activity", response_model=list[DeckWithActivityView])
async def list_decks_with_activity(db: DbDep) -> list[DeckWithActivityView]:
    from mpc_forge.models import DeckActivity as _DA

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

    activity_counts: dict[int, int] = dict(
        (
            await db.execute(
                select(_DA.deck_id, func.count(_DA.id))
                .where(_DA.deck_id.isnot(None))
                .group_by(_DA.deck_id)
            )
        ).all()
    )

    last_id_subq = (
        select(func.max(_DA.id).label("last_id"), _DA.deck_id.label("d"))
        .where(_DA.deck_id.isnot(None))
        .group_by(_DA.deck_id)
        .subquery()
    )
    last_rows = (
        await db.execute(
            select(_DA.deck_id, _DA.created_at, _DA.kind, _DA.summary).join(
                last_id_subq, _DA.id == last_id_subq.c.last_id
            )
        )
    ).all()
    last_activity: dict[int, tuple[datetime, str, str]] = {
        deck_id: (created_at, kind, summary) for deck_id, created_at, kind, summary in last_rows
    }

    covers = await deck_covers.covers_for_decks(db, [d for d, _ in deck_rows])

    out: list[DeckWithActivityView] = []
    for deck, card_count in deck_rows:
        cover = covers.get(deck.id, deck_covers.EMPTY)
        commander_name = cover.name
        commander_image = cover.image_url

        last = last_activity.get(deck.id)
        out.append(
            DeckWithActivityView(
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
            )
        )
    return out


@router.get("/{deck_id}/activity", response_model=list[ActivityEntry])
async def list_activity(
    deck_id: int,
    db: DbDep,
    kinds: str | None = None,
    limit: int = 500,
) -> list[ActivityEntry]:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    limit = max(1, min(limit, 2000))
    rows = await deck_activity.list_for_deck(db, deck_id, kinds=kinds_list, limit=limit)

    def _parse_payload(raw: str) -> dict:
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
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

    return UndoResponse(ok=True, summary=result["summary"], deck_id=deck_id)


@router.get("/_/undoable-kinds")
async def get_undoable_kinds(response: Response) -> list[str]:
    from mpc_forge.services import undo as undo_svc

    response.headers["Cache-Control"] = "public, max-age=3600"
    return sorted(undo_svc.UNDOABLE_KINDS)
