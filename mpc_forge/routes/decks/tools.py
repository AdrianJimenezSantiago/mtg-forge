from __future__ import annotations

from typing import Any, Literal

from fastapi import status
from pydantic import BaseModel, Field

from mpc_forge.clients.import_sites.base import ImportSiteError
from mpc_forge.errors import ApiError, from_exception
from mpc_forge.models import Deck
from mpc_forge.routes.decks._common import make_router
from mpc_forge.routes.dependencies import DbDep, ScryfallDep
from mpc_forge.services.decks import deck_tools

router = make_router()


class BulkEditRequest(BaseModel):
    card_ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["move", "include", "exclude", "delete"]
    role: str | None = None


class BulkEditResponse(BaseModel):
    changed: int


class SyncChangeView(BaseModel):
    key: str
    name: str
    role: str
    change: str
    before: int
    after: int
    scryfall_id: str | None = None


class SyncPreview(BaseModel):
    site: str
    roles: list[str]
    changes: list[SyncChangeView]
    unresolved: list[dict[str, Any]]


class SyncApplyRequest(BaseModel):
    keys: list[str] | None = None


class SyncApplyResponse(BaseModel):
    applied: int
    added: int = 0
    removed: int = 0
    quantity: int = 0
    snapshot_id: int | None = None


async def _deck(db: DbDep, deck_id: int) -> Deck:
    deck = await db.get(Deck, deck_id)
    if deck is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "deck_not_found")
    return deck


@router.post("/{deck_id}/cards/bulk", response_model=BulkEditResponse)
async def bulk_edit_cards(deck_id: int, payload: BulkEditRequest, db: DbDep) -> BulkEditResponse:
    await _deck(db, deck_id)
    try:
        changed = await deck_tools.bulk_edit(
            db, deck_id, payload.card_ids, payload.action, payload.role
        )
    except deck_tools.DeckToolError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "invalid_role") from e
    return BulkEditResponse(changed=changed)


@router.get("/{deck_id}/compare/{other_id}")
async def compare_decks(deck_id: int, other_id: int, db: DbDep) -> dict[str, Any]:
    return await deck_tools.compare(db, await _deck(db, deck_id), await _deck(db, other_id))


async def _plan(db: DbDep, scryfall: ScryfallDep, deck: Deck) -> deck_tools.SyncPlan:
    try:
        return await deck_tools.plan_sync(db, scryfall, deck)
    except deck_tools.DeckToolError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "no_source_url") from e
    except ImportSiteError as e:
        raise from_exception(status.HTTP_502_BAD_GATEWAY, e, "import_failed") from e


@router.get("/{deck_id}/sync/preview", response_model=SyncPreview)
async def sync_preview(deck_id: int, db: DbDep, scryfall: ScryfallDep) -> SyncPreview:
    plan = await _plan(db, scryfall, await _deck(db, deck_id))
    return SyncPreview(
        site=plan.site,
        roles=plan.roles,
        changes=[SyncChangeView(**c.to_dict()) for c in plan.changes],
        unresolved=plan.unresolved,
    )


@router.post("/{deck_id}/sync/apply", response_model=SyncApplyResponse)
async def sync_apply(
    deck_id: int, payload: SyncApplyRequest, db: DbDep, scryfall: ScryfallDep
) -> SyncApplyResponse:
    deck = await _deck(db, deck_id)
    plan = await _plan(db, scryfall, deck)
    keys = set(payload.keys) if payload.keys is not None else None
    return SyncApplyResponse(**await deck_tools.apply_sync(db, deck, plan, keys))
