from __future__ import annotations

import logging
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, File, Form, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import CursorResult, delete, func, select

from mpc_forge.errors import ApiError, from_exception
from mpc_forge.models import CollectionEntry
from mpc_forge.routes.dependencies import DbDep, ScryfallDep
from mpc_forge.services.cards import collection_io, images, printings, sets

router = APIRouter(prefix="/api/collection", tags=["collection"])
log = logging.getLogger(__name__)


WANTED_SET_TYPES = frozenset(
    {
        "core",
        "expansion",
        "masters",
        "draft_innovation",
        "commander",
        "funny",
        "starter",
    }
)


class SetInfo(BaseModel):
    code: str
    name: str
    set_type: str
    released_at: str | None = None
    card_count: int = 0
    icon_svg_uri: str | None = None
    owned_count: int = 0


@router.get("/sets")
async def list_sets(
    db: DbDep,
    scryfall: ScryfallDep,
) -> list[SetInfo]:
    every_set = await sets.all_sets(db, scryfall)
    sets.prefetch_icons(every_set)
    sets_data = sorted(
        (
            {**s, "icon_svg_uri": sets.local_icon_url(s.get("icon_svg_uri"))}
            for s in every_set
            if s.get("set_type") in WANTED_SET_TYPES and s.get("card_count", 0) > 0
        ),
        key=lambda x: x.get("released_at") or "",
        reverse=True,
    )

    owned_q = await db.execute(
        select(CollectionEntry.set_code, func.count()).group_by(CollectionEntry.set_code)
    )
    owned_map = dict(owned_q.all())

    return [
        SetInfo(
            **s,
            owned_count=owned_map.get(s["code"], 0),
        )
        for s in sets_data
    ]


class SetCardInfo(BaseModel):
    scryfall_id: str
    oracle_id: str
    name: str
    collector_number: str
    rarity: str
    image_small: str | None = None
    image_normal: str | None = None
    owned: bool = False
    quantity: int = 0
    foil_quantity: int = 0


@router.get("/set-icons/{name}.svg")
async def set_icon(name: str, db: DbDep, scryfall: ScryfallDep) -> FileResponse:
    try:
        path = await sets.icon_file(name, await sets.all_sets(db, scryfall))
    except httpx.HTTPError as e:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, "icon_download_failed", reason=str(e)) from e
    if path is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "unknown_icon")
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=604800",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/sets/{set_code}/cards")
async def set_cards(
    set_code: str,
    db: DbDep,
    scryfall: ScryfallDep,
) -> list[SetCardInfo]:
    all_cards = await printings.local_set_cards(db, set_code)
    if all_cards is None:
        all_cards = await scryfall.search_all(
            f"set:{set_code} -is:digital", unique="prints", order="set"
        )

    owned = {
        sid: (qty or 0, foil or 0)
        for sid, qty, foil in (
            await db.execute(
                select(
                    CollectionEntry.scryfall_id,
                    CollectionEntry.quantity,
                    CollectionEntry.foil_quantity,
                ).where(CollectionEntry.set_code == set_code)
            )
        ).all()
    }

    out = []
    for c in all_cards:
        sid = c.get("id", "")
        imgs = c.get("image_uris") or {}
        if not imgs and c.get("card_faces"):
            imgs = c["card_faces"][0].get("image_uris", {})
        out.append(
            SetCardInfo(
                scryfall_id=sid,
                oracle_id=c.get("oracle_id", ""),
                name=c.get("name", ""),
                collector_number=c.get("collector_number", ""),
                rarity=c.get("rarity", "common"),
                image_small=images.thumb(imgs),
                image_normal=images.grid(imgs),
                owned=sid in owned,
                quantity=owned.get(sid, (0, 0))[0],
                foil_quantity=owned.get(sid, (0, 0))[1],
            )
        )

    return out


class ToggleOwnedRequest(BaseModel):
    scryfall_id: str
    oracle_id: str
    name: str
    set_code: str
    set_name: str = ""
    collector_number: str = ""
    rarity: str = "common"
    image_small: str | None = None


class ToggleOwnedResponse(BaseModel):
    owned: bool
    set_owned_count: int


@router.post("/toggle-owned", response_model=ToggleOwnedResponse)
async def toggle_owned(payload: ToggleOwnedRequest, db: DbDep) -> ToggleOwnedResponse:
    existing = await db.get(CollectionEntry, payload.scryfall_id)
    if existing:
        await db.delete(existing)
        await db.flush()
        owned = False
    else:
        entry = CollectionEntry(
            scryfall_id=payload.scryfall_id,
            oracle_id=payload.oracle_id,
            name=payload.name,
            set_code=payload.set_code,
            set_name=payload.set_name,
            collector_number=payload.collector_number,
            rarity=payload.rarity,
            image_small=payload.image_small,
        )
        db.add(entry)
        await db.flush()
        owned = True

    count = (
        await db.scalar(
            select(func.count())
            .select_from(CollectionEntry)
            .where(CollectionEntry.set_code == payload.set_code)
        )
    ) or 0

    await db.commit()
    return ToggleOwnedResponse(owned=owned, set_owned_count=count)


class BatchToggleRequest(BaseModel):
    set_code: str
    set_name: str = ""
    cards: list[ToggleOwnedRequest]
    action: str = "add"


class BatchToggleResponse(BaseModel):
    added: int = 0
    removed: int = 0
    set_owned_count: int = 0


@router.post("/batch-toggle", response_model=BatchToggleResponse)
async def batch_toggle(payload: BatchToggleRequest, db: DbDep) -> BatchToggleResponse:
    added = 0
    removed = 0

    if payload.action == "remove":
        ids = [c.scryfall_id for c in payload.cards]
        result = await db.execute(
            delete(CollectionEntry).where(CollectionEntry.scryfall_id.in_(ids))
        )
        removed = cast(CursorResult[Any], result).rowcount
    else:
        existing = set(
            (
                await db.scalars(
                    select(CollectionEntry.scryfall_id).where(
                        CollectionEntry.scryfall_id.in_([c.scryfall_id for c in payload.cards])
                    )
                )
            ).all()
        )
        for c in payload.cards:
            if c.scryfall_id not in existing:
                db.add(
                    CollectionEntry(
                        scryfall_id=c.scryfall_id,
                        oracle_id=c.oracle_id,
                        name=c.name,
                        set_code=c.set_code,
                        set_name=c.set_name or payload.set_name,
                        collector_number=c.collector_number,
                        rarity=c.rarity,
                        image_small=c.image_small,
                    )
                )
                added += 1

    await db.flush()

    count = (
        await db.scalar(
            select(func.count())
            .select_from(CollectionEntry)
            .where(CollectionEntry.set_code == payload.set_code)
        )
    ) or 0

    await db.commit()
    return BatchToggleResponse(added=added, removed=removed, set_owned_count=count)


class CollectionStats(BaseModel):
    total_owned: int = 0
    unique_sets: int = 0
    total_copies: int = 0
    foil_copies: int = 0


@router.get("/stats", response_model=CollectionStats)
async def collection_stats(db: DbDep) -> CollectionStats:
    total = (await db.scalar(select(func.count()).select_from(CollectionEntry))) or 0
    sets = (await db.scalar(select(func.count(func.distinct(CollectionEntry.set_code))))) or 0
    normal, foil = (
        await db.execute(
            select(
                func.coalesce(func.sum(CollectionEntry.quantity), 0),
                func.coalesce(func.sum(CollectionEntry.foil_quantity), 0),
            )
        )
    ).one()
    return CollectionStats(
        total_owned=total,
        unique_sets=sets,
        total_copies=int(normal) + int(foil),
        foil_copies=int(foil),
    )


class SetQuantityRequest(ToggleOwnedRequest):
    quantity: int = Field(ge=0, le=collection_io.MAX_QUANTITY)
    foil_quantity: int = Field(default=0, ge=0, le=collection_io.MAX_QUANTITY)


class SetQuantityResponse(BaseModel):
    owned: bool
    quantity: int
    foil_quantity: int
    set_owned_count: int


@router.post("/set-quantity", response_model=SetQuantityResponse)
async def set_quantity(payload: SetQuantityRequest, db: DbDep) -> SetQuantityResponse:
    fields = payload.model_dump(exclude={"quantity", "foil_quantity"})
    entry = await collection_io.set_quantity(db, fields, payload.quantity, payload.foil_quantity)
    await db.flush()
    count = (
        await db.scalar(
            select(func.count())
            .select_from(CollectionEntry)
            .where(CollectionEntry.set_code == payload.set_code)
        )
    ) or 0
    await db.commit()
    return SetQuantityResponse(
        owned=entry is not None,
        quantity=entry.quantity if entry else 0,
        foil_quantity=entry.foil_quantity if entry else 0,
        set_owned_count=count,
    )


class ImportReportView(BaseModel):
    rows: int
    cards: int
    printings: int
    added: int
    updated: int
    removed: int
    unresolved: list[dict[str, Any]]


MAX_CSV_BYTES = 20 * 1024 * 1024


@router.post("/import", response_model=ImportReportView)
async def import_collection(
    db: DbDep,
    scryfall: ScryfallDep,
    file: Annotated[UploadFile, File()],
    mode: Annotated[Literal["merge", "replace"], Form()] = "merge",
) -> ImportReportView:
    raw = await file.read(MAX_CSV_BYTES + 1)
    await file.close()
    if len(raw) > MAX_CSV_BYTES:
        raise ApiError(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "collection_csv_too_large")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        report = await collection_io.import_csv(db, scryfall, text, mode)
    except collection_io.CollectionImportError as e:
        raise from_exception(status.HTTP_400_BAD_REQUEST, e, "collection_import_failed") from e
    return ImportReportView(**report.to_dict())


@router.get("/export.csv")
async def export_collection(db: DbDep) -> Response:
    return Response(
        content=await collection_io.export_csv(db),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="mpc-forge-collection.csv"'},
    )
