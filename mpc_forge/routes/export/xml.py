from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select

from mpc_forge import config as cfg
from mpc_forge.config import DEFAULT_CARDSTOCK
from mpc_forge.models import DeckCard
from mpc_forge.routes.dependencies import ArtCacheDep, DbDep, ScryfallDep
from mpc_forge.routes.export._shared import (
    export_path,
    get_deck_or_404,
    make_router,
    resolve_with_progress,
)
from mpc_forge.routes.export.cardbacks import resolve_deck_cardback
from mpc_forge.schemas import BuildXMLRequest
from mpc_forge.services.decks import deck_activity
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K
from mpc_forge.services.printing import build_progress, cost_estimator, history
from mpc_forge.services.printing.print_runs import (
    split_into_runs,
    split_into_runs_optimized,
    summary_dict,
)
from mpc_forge.services.printing.xml_generator import (
    build_xml,
    default_cardback_path,
    plan_deck_slots,
    resolve_deck_for_xml,
)

router = make_router()

ESTIMATED_ROLES = ("commander", "mainboard")


class EstimateResponse(BaseModel):
    total_cards: int
    tier_size: int
    unit_usd: float
    subtotal_usd: float
    per_card_effective_usd: float
    subtotal_eur: float
    shipping_eur: float
    shipping_base_eur: float
    shipping_eu_extra_eur: float
    total_eur: float
    per_card_effective_eur: float
    next_tier_size: int | None = None
    cards_to_next_tier: int | None = None
    next_tier_subtotal_usd: float | None = None
    next_tier_subtotal_eur: float | None = None
    next_tier_total_eur: float | None = None
    next_tier_saves_eur: float | None = None


@router.get("/decks/{deck_id}/estimate", response_model=EstimateResponse)
async def estimate_deck(deck_id: int, db: DbDep) -> EstimateResponse:
    await get_deck_or_404(db, deck_id)
    total = await db.scalar(
        select(func.coalesce(func.sum(DeckCard.quantity), 0)).where(
            DeckCard.deck_id == deck_id,
            DeckCard.include.is_(True),
            DeckCard.role.in_(ESTIMATED_ROLES),
        )
    )
    est = cost_estimator.estimate(int(total or 0))
    return EstimateResponse(**est.__dict__)


class XMLBuildResponse(BaseModel):
    xml_path: str
    total_cards: int
    tier_size: int
    estimated_cost_eur: float
    run_id: int | None


@router.post("/decks/{deck_id}/build-xml", response_model=XMLBuildResponse)
async def build_xml_endpoint(
    deck_id: int,
    payload: BuildXMLRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    art_cache: ArtCacheDep,
) -> XMLBuildResponse:
    deck = await get_deck_or_404(db, deck_id)
    resolved = await resolve_with_progress(db, scryfall, art_cache, deck, kind="xml")

    cardstock = payload.cardstock or DEFAULT_CARDSTOCK
    foil = bool(payload.foil)
    result = await asyncio.to_thread(
        build_xml,
        cards=resolved,
        output_path=export_path(deck.name, ".xml"),
        cardstock=cardstock,
        foil=foil,
        cardback_path=default_cardback_path(),
        web_mode=payload.web_mode,
    )
    build_progress.finish(deck_id)

    est = cost_estimator.estimate(result.total_cards)

    run_id: int | None = None
    if payload.create_run:
        run = await history.create_print_run_from_deck(
            db,
            deck=deck,
            cardstock=cardstock,
            foil=foil,
            tier_size=est.tier_size,
            estimated_cost_eur=est.total_eur,
            xml_path=str(result.xml_path),
            run_name=payload.run_name,
        )
        run_id = run.id

    await deck_activity.log_event(
        db,
        deck_id,
        K.XML_GENERATED,
        payload={
            "cardstock": cardstock,
            "foil": foil,
            "total_cards": result.total_cards,
            "tier_size": est.tier_size,
            "estimated_cost_eur": est.total_eur,
            "xml_path": str(result.xml_path),
            "xml_filename": Path(str(result.xml_path)).name,
            "run_id": run_id,
        },
        deck_name=deck.name,
    )
    await db.commit()

    return XMLBuildResponse(
        xml_path=str(result.xml_path),
        total_cards=result.total_cards,
        tier_size=est.tier_size,
        estimated_cost_eur=est.total_eur,
        run_id=run_id,
    )


class PrintRunPlanCardView(BaseModel):
    name: str
    quantity: int
    scryfall_id: str
    has_back: bool


class PrintRunPlanView(BaseModel):
    run_index: int
    tier_size: int
    unit_usd: float
    subtotal_usd: float
    total_cards: int
    wasted_slots: int
    cards: list[PrintRunPlanCardView]


class PrintRunSplitResponse(BaseModel):
    total_runs: int
    total_cards: int
    total_wasted_slots: int
    total_subtotal_usd: float
    runs: list[PrintRunPlanView]


@router.get("/decks/{deck_id}/print-runs/preview", response_model=PrintRunSplitResponse)
async def preview_print_runs(
    deck_id: int,
    db: DbDep,
    max_tier: int | None = None,
    optimize: bool = False,
) -> PrintRunSplitResponse:
    deck = await get_deck_or_404(db, deck_id)
    slots = await plan_deck_slots(db, deck)
    if not slots:
        return PrintRunSplitResponse(
            total_runs=0,
            total_cards=0,
            total_wasted_slots=0,
            total_subtotal_usd=0.0,
            runs=[],
        )
    plan = (
        split_into_runs_optimized(slots) if optimize else split_into_runs(slots, max_tier=max_tier)
    )
    return PrintRunSplitResponse(**summary_dict(plan))


class BuildSplitXMLRequest(BaseModel):
    cardstock: str | None = None
    foil: bool = False
    max_tier: int | None = None
    create_runs: bool = True
    web_mode: bool = False


class BuildSplitXMLResponse(BaseModel):
    total_runs: int
    xml_paths: list[str]
    total_cards: int
    total_subtotal_usd: float
    run_ids: list[int]


@router.post("/decks/{deck_id}/build-split-xml", response_model=BuildSplitXMLResponse)
async def build_split_xml_endpoint(
    deck_id: int,
    payload: BuildSplitXMLRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    art_cache: ArtCacheDep,
) -> BuildSplitXMLResponse:
    deck = await get_deck_or_404(db, deck_id)

    resolved = await resolve_deck_for_xml(db, scryfall, art_cache, deck)
    if not resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    plan = split_into_runs(resolved, max_tier=payload.max_tier)
    if not plan.runs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El split no produjo runs")

    cardstock = payload.cardstock or DEFAULT_CARDSTOCK
    cardback = await resolve_deck_cardback(db, deck)
    base_path = export_path(deck.name, "")

    xml_paths: list[str] = []
    run_ids: list[int] = []
    for run_plan in plan.runs:
        run_number = run_plan.run_index + 1
        out_path = base_path.with_name(f"{base_path.name}-run{run_number}of{plan.total_runs}.xml")
        result = await asyncio.to_thread(
            build_xml,
            cards=run_plan.cards,
            output_path=out_path,
            cardstock=cardstock,
            foil=payload.foil,
            cardback_path=cardback,
            web_mode=payload.web_mode,
        )
        xml_paths.append(str(result.xml_path))

        if payload.create_runs:
            run_row = await history.create_print_run_from_deck(
                db,
                deck=deck,
                cardstock=cardstock,
                foil=payload.foil,
                tier_size=run_plan.tier_size,
                estimated_cost_eur=run_plan.subtotal_usd * cfg.USD_TO_EUR,
                xml_path=str(result.xml_path),
                run_name=f"{deck.name} · run {run_number}/{plan.total_runs}",
            )
            run_ids.append(run_row.id)

    await db.commit()
    return BuildSplitXMLResponse(
        total_runs=plan.total_runs,
        xml_paths=xml_paths,
        total_cards=plan.total_cards,
        total_subtotal_usd=plan.total_subtotal_usd,
        run_ids=run_ids,
    )
