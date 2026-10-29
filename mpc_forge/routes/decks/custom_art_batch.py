from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from mpc_forge.models import CustomArtFolder
from mpc_forge.routes.decks._common import make_router
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.art import custom_art, custom_art_folders
from mpc_forge.services.art import custom_art_batch as batch

router = make_router()


class PlanCandidate(BaseModel):
    ref: int
    filename: str
    variant: str | None
    face: str
    thumb_url: str | None


class PlanItemView(BaseModel):
    deck_card_id: int
    name: str
    role: str
    quantity: int
    face: Literal["front", "back"]
    status: Literal["match", "same", "keep", "missing"]
    current_custom_art_id: int | None
    proposed: int | None
    candidates: list[PlanCandidate]


class UnusedImageView(PlanCandidate):
    suggestions: list[int]


class PlanSummary(BaseModel):
    cards: int
    match: int
    same: int
    keep: int
    missing: int
    backs: int
    unused: int
    rejected: int


class PlanView(BaseModel):
    deck_id: int
    source: Literal["library", "files"]
    summary: PlanSummary
    items: list[PlanItemView]
    unused: list[UnusedImageView]
    rejected: list[str]


class LibraryPlanRequest(BaseModel):
    overwrite: bool = False
    rescan: bool = False
    art_ids: list[int] | None = Field(default=None, max_length=batch.MAX_FILENAMES)
    folder_id: int | None = None


class FilesPlanRequest(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=batch.MAX_FILENAMES)
    overwrite: bool = False


class AssignmentIn(BaseModel):
    deck_card_id: int
    face: Literal["front", "back"] = "front"
    custom_art_id: int


class ApplyRequest(BaseModel):
    assignments: list[AssignmentIn] = Field(max_length=4 * batch.MAX_FILENAMES)


class ApplyResultView(BaseModel):
    deck_id: int
    changed: int
    unchanged: int
    invalid: int
    snapshot_id: int | None
    event_id: int | None


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")


@router.post("/{deck_id}/custom-art/plan", response_model=PlanView)
async def plan_from_library(deck_id: int, payload: LibraryPlanRequest, db: DbDep) -> PlanView:
    if payload.rescan:
        await custom_art.rescan(db)
    art_ids = payload.art_ids
    if payload.folder_id is not None:
        if await db.get(CustomArtFolder, payload.folder_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Carpeta no encontrada")
        art_ids = await custom_art_folders.folder_art_ids(db, payload.folder_id)
    try:
        plan = await batch.plan_from_library(
            db, deck_id, overwrite=payload.overwrite, art_ids=art_ids
        )
    except batch.DeckNotFound as e:
        raise _not_found() from e
    return PlanView.model_validate(plan.to_dict())


@router.post("/{deck_id}/custom-art/plan-files", response_model=PlanView)
async def plan_from_files(deck_id: int, payload: FilesPlanRequest, db: DbDep) -> PlanView:
    try:
        plan = await batch.plan_from_filenames(
            db, deck_id, payload.filenames, overwrite=payload.overwrite
        )
    except batch.DeckNotFound as e:
        raise _not_found() from e
    return PlanView.model_validate(plan.to_dict())


@router.post("/{deck_id}/custom-art/apply", response_model=ApplyResultView)
async def apply_custom_art(deck_id: int, payload: ApplyRequest, db: DbDep) -> ApplyResultView:
    assignments = [
        batch.Assignment(deck_card_id=a.deck_card_id, face=a.face, custom_art_id=a.custom_art_id)
        for a in payload.assignments
    ]
    try:
        result = await batch.apply(db, deck_id, assignments)
    except batch.DeckNotFound as e:
        raise _not_found() from e
    return ApplyResultView(**result.to_dict())
