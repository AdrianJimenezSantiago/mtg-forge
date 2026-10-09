from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.system import tour as tour_service

router = APIRouter(prefix="/api/tour", tags=["tour"])


class TourState(BaseModel):
    seen: list[str]
    auto: bool


class TourSeenRequest(BaseModel):
    tour: str = Field(..., min_length=1, max_length=41)


class TourAutoRequest(BaseModel):
    auto: bool


@router.get("/", response_model=TourState)
async def get_tour_state(db: DbDep) -> TourState:
    return TourState(**await tour_service.get_state(db))


@router.post("/seen", response_model=TourState)
async def mark_tour_seen(payload: TourSeenRequest, db: DbDep) -> TourState:
    try:
        return TourState(**await tour_service.mark_seen(db, payload.tour))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.put("/auto", response_model=TourState)
async def set_tour_auto(payload: TourAutoRequest, db: DbDep) -> TourState:
    return TourState(**await tour_service.set_auto(db, payload.auto))


@router.delete("/", response_model=TourState)
async def reset_tour(db: DbDep) -> TourState:
    return TourState(**await tour_service.reset(db))
