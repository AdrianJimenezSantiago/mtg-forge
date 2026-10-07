from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.art import art_themes
from mpc_forge.services.decks import snapshots

log = logging.getLogger(__name__)

router = APIRouter(tags=["planner"])


class CreateThemeRequest(BaseModel):
    deck_id: int
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=1024)
    only_customized: bool = True


class RenameThemeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None


class ApplyThemeRequest(BaseModel):
    deck_id: int
    overwrite_custom: bool = True
    create_snapshot: bool = True


@router.get("/api/art-themes")
async def list_art_themes(db: DbDep) -> dict[str, Any]:
    return {"themes": await art_themes.list_themes(db)}


@router.get("/api/art-themes/{theme_id}")
async def get_art_theme(theme_id: int, db: DbDep) -> dict[str, Any]:
    theme = await art_themes.get_theme(db, theme_id)
    if theme is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema no encontrado")
    return theme


@router.post("/api/art-themes", status_code=status.HTTP_201_CREATED)
async def create_art_theme(payload: CreateThemeRequest, db: DbDep) -> dict[str, Any]:
    theme = await art_themes.create_from_deck(
        db,
        payload.deck_id,
        payload.name,
        description=payload.description,
        only_customized=payload.only_customized,
    )
    if theme is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return theme


@router.patch("/api/art-themes/{theme_id}")
async def rename_art_theme(theme_id: int, payload: RenameThemeRequest, db: DbDep) -> dict[str, Any]:
    theme = await art_themes.rename_theme(db, theme_id, payload.name, payload.description)
    if theme is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema no encontrado")
    return theme


@router.delete("/api/art-themes/{theme_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_art_theme(theme_id: int, db: DbDep) -> None:
    if not await art_themes.delete_theme(db, theme_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema no encontrado")


@router.get("/api/art-themes/{theme_id}/preview/{deck_id}")
async def preview_art_theme(theme_id: int, deck_id: int, db: DbDep) -> dict[str, Any]:
    preview = await art_themes.preview_apply(db, theme_id, deck_id)
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema o mazo no encontrado")
    return preview


@router.post("/api/art-themes/{theme_id}/apply")
async def apply_art_theme(theme_id: int, payload: ApplyThemeRequest, db: DbDep) -> dict[str, Any]:
    snapshot = None
    if payload.create_snapshot:
        snapshot = await snapshots.create(
            db, payload.deck_id, label="Antes de aplicar un tema", auto=True
        )

    result = await art_themes.apply_to_deck(
        db,
        theme_id,
        payload.deck_id,
        overwrite_custom=payload.overwrite_custom,
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema o mazo no encontrado")
    return {**result.to_dict(), "snapshot": snapshot}
