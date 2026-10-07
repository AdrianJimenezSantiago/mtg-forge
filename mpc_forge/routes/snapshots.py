from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.decks import snapshots

log = logging.getLogger(__name__)

router = APIRouter(tags=["planner"])


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
    result = await snapshots.restore(db, snapshot_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
    return result


@router.get("/api/snapshots/{snapshot_id}/diff")
async def diff_snapshot(snapshot_id: int, db: DbDep, against: int | None = None) -> dict[str, Any]:
    result = await snapshots.diff(db, snapshot_id, against)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
    return result


@router.delete("/api/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot(snapshot_id: int, db: DbDep) -> None:
    if not await snapshots.delete_snapshot(db, snapshot_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Snapshot no encontrado")
