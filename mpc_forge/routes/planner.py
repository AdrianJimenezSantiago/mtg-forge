"""Endpoints del Sprint 4: planificador, diff de colección, temas y snapshots.

Se agrupan en un módulo propio en lugar de repartirlos por los routers
existentes porque son funcionalidad nueva y transversal: el planificador cruza
mazos con colección y con tiers de precio, y no pertenece a ninguno de los
tres.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.db import get_session
from mpc_forge.services import art_themes, print_needs, print_planner, snapshots

log = logging.getLogger(__name__)
DbDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(tags=["planner"])


# ---------------------------------------------------------------------------
# Planificador de tiradas
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    deck_ids: list[int] = Field(default_factory=list, max_length=50)
    # Techo por pedido. None = usar el tier máximo de MPC.
    max_tier: int | None = None
    keep_decks_together: bool = True
    # Si se activa, se descuentan las cartas que el usuario ya posee.
    subtract_collection: bool = False
    match_mode: Literal["oracle", "exact"] = "oracle"
    include_basics: bool = True


@router.post("/api/planner/plan")
async def build_plan(payload: PlanRequest, db: DbDep) -> dict[str, Any]:
    """Reparte los mazos seleccionados en pedidos de MPC.

    Con ``subtract_collection`` el plan se calcula sobre lo que falta por
    imprimir, no sobre el mazo entero: es la pregunta que de verdad se hace
    quien ya tiene parte de las cartas.
    """
    if not payload.deck_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Selecciona al menos un mazo"
        )

    if not payload.subtract_collection:
        plan = await print_planner.build_plan(
            db, payload.deck_ids,
            max_tier=payload.max_tier,
            keep_decks_together=payload.keep_decks_together,
        )
        return {**plan.to_dict(), "subtracted_collection": False}

    # Con la colección descontada, las contribuciones ya no son "el mazo
    # entero" sino "lo que falta", así que se construyen a mano en vez de
    # dejar que el planificador las lea de la BD.
    needs = await print_needs.compute_for_decks(
        db, payload.deck_ids,
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
    """Tiers de MPC configurados. La vista los usa para el selector de techo."""
    return {
        "tiers": [
            {"size": int(t["size"]), "unit_usd": float(t["unit_usd"])}
            for t in print_planner.tiers()
        ],
        "max_size": print_planner.max_tier_size(),
    }


@router.get("/api/planner/compare")
async def compare(
    total_cards: int = Query(..., ge=1, le=100000)
) -> dict[str, Any]:
    """Coste de repartir N cartas con distintos techos por pedido."""
    return {"options": print_planner.compare_alternatives(total_cards)}


# ---------------------------------------------------------------------------
# Diff colección ↔ mazo
# ---------------------------------------------------------------------------

@router.get("/api/decks/{deck_id}/print-needs")
async def deck_print_needs(
    deck_id: int,
    db: DbDep,
    match_mode: Literal["oracle", "exact"] = "oracle",
    include_basics: bool = True,
) -> dict[str, Any]:
    """Qué falta por imprimir de un mazo, descontando la colección."""
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
    # Reparte las copias poseídas entre los mazos en vez de contarlas para
    # todos. Ver la explicación en services/print_needs.py.
    shared_collection: bool = True


@router.post("/api/planner/print-needs")
async def multi_deck_needs(
    payload: MultiNeedsRequest, db: DbDep
) -> dict[str, Any]:
    if not payload.deck_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Selecciona al menos un mazo"
        )
    needs = await print_needs.compute_for_decks(
        db, payload.deck_ids,
        match_mode=payload.match_mode,
        include_basics=payload.include_basics,
        shared_collection=payload.shared_collection,
    )
    return {
        "decks": [n.to_dict(include_cards=False) for n in needs],
        "summary": print_needs.needs_summary(needs),
    }


# ---------------------------------------------------------------------------
# Temas de arte
# ---------------------------------------------------------------------------

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
    # Snapshot previo: aplicar un tema toca decenas de cartas de golpe y debe
    # poder deshacerse de un clic.
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
async def create_art_theme(
    payload: CreateThemeRequest, db: DbDep
) -> dict[str, Any]:
    theme = await art_themes.create_from_deck(
        db, payload.deck_id, payload.name,
        description=payload.description,
        only_customized=payload.only_customized,
    )
    if theme is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return theme


@router.patch("/api/art-themes/{theme_id}")
async def rename_art_theme(
    theme_id: int, payload: RenameThemeRequest, db: DbDep
) -> dict[str, Any]:
    theme = await art_themes.rename_theme(
        db, theme_id, payload.name, payload.description
    )
    if theme is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema no encontrado")
    return theme


@router.delete("/api/art-themes/{theme_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_art_theme(theme_id: int, db: DbDep) -> None:
    if not await art_themes.delete_theme(db, theme_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema no encontrado")


@router.get("/api/art-themes/{theme_id}/preview/{deck_id}")
async def preview_art_theme(
    theme_id: int, deck_id: int, db: DbDep
) -> dict[str, Any]:
    """Qué cambiaría al aplicar, sin tocar nada."""
    preview = await art_themes.preview_apply(db, theme_id, deck_id)
    if preview is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema o mazo no encontrado")
    return preview


@router.post("/api/art-themes/{theme_id}/apply")
async def apply_art_theme(
    theme_id: int, payload: ApplyThemeRequest, db: DbDep
) -> dict[str, Any]:
    snapshot = None
    if payload.create_snapshot:
        snapshot = await snapshots.create(
            db, payload.deck_id, label="Antes de aplicar un tema", auto=True
        )

    result = await art_themes.apply_to_deck(
        db, theme_id, payload.deck_id,
        overwrite_custom=payload.overwrite_custom,
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tema o mazo no encontrado")
    return {**result.to_dict(), "snapshot": snapshot}


# ---------------------------------------------------------------------------
# Snapshots de mazo
# ---------------------------------------------------------------------------

class CreateSnapshotRequest(BaseModel):
    label: str = Field("", max_length=256)


@router.get("/api/decks/{deck_id}/snapshots")
async def list_snapshots(deck_id: int, db: DbDep) -> dict[str, Any]:
    return {"snapshots": await snapshots.list_for_deck(db, deck_id)}


@router.post("/api/decks/{deck_id}/snapshots", status_code=status.HTTP_201_CREATED)
async def create_snapshot(
    deck_id: int, payload: CreateSnapshotRequest, db: DbDep
) -> dict[str, Any]:
    snapshot = await snapshots.create(db, deck_id, label=payload.label)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    return snapshot


@router.post("/api/snapshots/{snapshot_id}/restore")
async def restore_snapshot(snapshot_id: int, db: DbDep) -> dict[str, Any]:
    """Devuelve el mazo al estado del snapshot.

    El estado actual se guarda antes como snapshot automático: restaurar por
    error no debe ser irreversible.
    """
    result = await snapshots.restore(db, snapshot_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
    return result


@router.get("/api/snapshots/{snapshot_id}/diff")
async def diff_snapshot(
    snapshot_id: int, db: DbDep, against: int | None = None
) -> dict[str, Any]:
    """Compara con otro snapshot, o con el estado actual si no se indica."""
    result = await snapshots.diff(db, snapshot_id, against)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
    return result


@router.delete(
    "/api/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_snapshot(snapshot_id: int, db: DbDep) -> None:
    if not await snapshots.delete_snapshot(db, snapshot_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
