from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.printing import print_needs, print_planner

log = logging.getLogger(__name__)

router = APIRouter(tags=["planner"])


class PlanRequest(BaseModel):
    deck_ids: list[int] = Field(default_factory=list, max_length=50)
    max_tier: int | None = None
    keep_decks_together: bool = True
    subtract_collection: bool = False
    match_mode: Literal["oracle", "exact"] = "oracle"
    include_basics: bool = True


@router.post("/api/planner/plan")
async def build_plan(payload: PlanRequest, db: DbDep) -> dict[str, Any]:
    if not payload.deck_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selecciona al menos un mazo")

    if not payload.subtract_collection:
        plan = await print_planner.build_plan(
            db,
            payload.deck_ids,
            max_tier=payload.max_tier,
            keep_decks_together=payload.keep_decks_together,
        )
        return {**plan.to_dict(), "subtracted_collection": False}

    needs = await print_needs.compute_for_decks(
        db,
        payload.deck_ids,
        match_mode=payload.match_mode,
        include_basics=payload.include_basics,
        shared_collection=True,
    )
    contributions = [
        print_planner.DeckContribution(
            deck_id=n.deck_id,
            name=n.deck_name,
            card_count=n.total_needed,
            distinct_cards=sum(1 for c in n.cards if c.needed > 0),
        )
        for n in needs
    ]
    runs = print_planner.plan_runs(
        contributions,
        max_tier=payload.max_tier,
        keep_decks_together=payload.keep_decks_together,
    )
    total = sum(r.card_count for r in runs)
    plan = print_planner.PlanResult(
        decks=contributions,
        runs=runs,
        alternatives=print_planner.compare_alternatives(total),
        filler=print_planner.suggest_filler(runs),
    )
    return {
        **plan.to_dict(),
        "subtracted_collection": True,
        "needs_summary": print_needs.needs_summary(needs),
    }


@router.get("/api/planner/tiers")
async def list_tiers() -> dict[str, Any]:
    return {
        "tiers": [
            {"size": int(t["size"]), "unit_usd": float(t["unit_usd"])}
            for t in print_planner.tiers()
        ],
        "max_size": print_planner.max_tier_size(),
    }


@router.get("/api/planner/compare")
async def compare(total_cards: int = Query(..., ge=1, le=100000)) -> dict[str, Any]:
    return {"options": print_planner.compare_alternatives(total_cards)}


@router.get("/api/decks/{deck_id}/print-needs")
async def deck_print_needs(
    deck_id: int,
    db: DbDep,
    match_mode: Literal["oracle", "exact"] = "oracle",
    include_basics: bool = True,
) -> dict[str, Any]:
    needs = await print_needs.compute_for_deck(
        db, deck_id, match_mode=match_mode, include_basics=include_basics
    )
    if needs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return needs.to_dict()


class MultiNeedsRequest(BaseModel):
    deck_ids: list[int] = Field(default_factory=list, max_length=50)
    match_mode: Literal["oracle", "exact"] = "oracle"
    include_basics: bool = True
    shared_collection: bool = True


@router.post("/api/planner/print-needs")
async def multi_deck_needs(payload: MultiNeedsRequest, db: DbDep) -> dict[str, Any]:
    if not payload.deck_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Selecciona al menos un mazo")
    needs = await print_needs.compute_for_decks(
        db,
        payload.deck_ids,
        match_mode=payload.match_mode,
        include_basics=payload.include_basics,
        shared_collection=payload.shared_collection,
    )
    return {
        "decks": [n.to_dict(include_cards=False) for n in needs],
        "summary": print_needs.needs_summary(needs),
    }
