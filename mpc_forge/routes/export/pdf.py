from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from mpc_forge.routes.dependencies import ArtCacheDep, DbDep, ScryfallDep
from mpc_forge.routes.export._shared import (
    export_path,
    get_deck_or_404,
    make_router,
    parse_decklist_format,
    resolve_with_progress,
)
from mpc_forge.routes.export.cardbacks import resolve_deck_cardback
from mpc_forge.services.decks import deck_activity, decklist_export
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K
from mpc_forge.services.printing import build_progress
from mpc_forge.services.printing.image_export import build_images_zip
from mpc_forge.services.printing.pdf_generator import PDFOptions, build_pdf

router = make_router()


class BuildPDFRequest(BaseModel):
    page_size: str = "a4"
    orientation: str = "portrait"
    cols: int = 3
    rows: int = 3

    gap_x_mm: float = 0.0
    gap_y_mm: float = 0.0

    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    back_offset_x_mm: float = 0.0
    back_offset_y_mm: float = 0.0

    bleed_enabled: bool = False
    bleed_mm: float = 0.0

    card_guides_enabled: bool = True
    card_guides_style: str = "corners"
    card_guides_shape: str = "square"
    card_guides_pattern: str = "solid"
    card_guides_placement: str = "outside"
    card_guides_length_mm: float = 4.0
    card_guides_color: str = "#606060"
    card_guides_width_pt: float = 0.4

    page_guides: str = "none"

    hide_card_guides_front: bool = False
    hide_card_guides_back: bool = False
    hide_page_guides_front: bool = False
    hide_page_guides_back: bool = False

    reg_marks_enabled: bool = False
    reg_marks_inset_mm: float = 10.0
    reg_marks_size_mm: float = 5.0

    include_backs: bool = False
    backs_layout: str = "append"
    backs_content: str = "all_cards"
    backs_compact_fill: bool = True

    page_range: str = ""

    show_footer: bool = True

    cut_marks: bool | None = None
    gap_mm: float | None = None
    guides_enabled: bool | None = None
    guides_style: str | None = None
    guides_stroke: str | None = None
    guides_placement: str | None = None
    guides_length_mm: float | None = None
    guides_color: str | None = None
    guides_width_pt: float | None = None


class PDFBuildResponse(BaseModel):
    pdf_path: str
    filename: str
    total_pages: int
    total_slots: int
    cols: int
    rows: int


class ImagesExportRequest(BaseModel):
    decklist_format: str = "with_set"


class ImagesExportResponse(BaseModel):
    zip_path: str
    filename: str
    total_files: int
    total_unique_cards: int
    total_dfc_backs: int
    included_cardback: bool
    missing_images: int
    size_bytes: int


class BuildProgressResponse(BaseModel):
    active: bool
    deck_id: int
    total: int
    current: int
    current_name: str
    kind: str
    done: bool
    error: str | None
    elapsed_seconds: float
    eta_seconds: float | None
    percent: float


_Choice = TypeVar("_Choice", bound=str)


def _one_of(value: str, allowed: tuple[_Choice, ...], default: _Choice) -> _Choice:
    v = (value or "").lower().strip()
    return next((choice for choice in allowed if choice == v), default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _legacy_or(legacy: Any, current: Any) -> Any:
    return legacy if legacy is not None else current


def _pdf_options(payload: BuildPDFRequest) -> PDFOptions:
    card_guides_enabled = payload.card_guides_enabled
    if payload.cut_marks is not None:
        card_guides_enabled = payload.cut_marks
    if payload.guides_enabled is not None:
        card_guides_enabled = payload.guides_enabled

    if payload.gap_mm is not None:
        gap_x = gap_y = payload.gap_mm
    else:
        gap_x, gap_y = payload.gap_x_mm, payload.gap_y_mm

    card_style = _legacy_or(payload.guides_style, payload.card_guides_style)
    card_pattern = _legacy_or(payload.guides_stroke, payload.card_guides_pattern)
    card_placement = _legacy_or(payload.guides_placement, payload.card_guides_placement)
    card_length = _legacy_or(payload.guides_length_mm, payload.card_guides_length_mm)
    card_color = _legacy_or(payload.guides_color, payload.card_guides_color)
    card_width = _legacy_or(payload.guides_width_pt, payload.card_guides_width_pt)

    return PDFOptions(
        page_size=_one_of(payload.page_size, ("a4", "letter", "a3"), "a4"),
        orientation=_one_of(payload.orientation, ("portrait", "landscape"), "portrait"),
        cols=max(0, min(20, payload.cols)),
        rows=max(0, min(20, payload.rows)),
        gap_x_mm=_clamp(gap_x, 0.0, 50.0),
        gap_y_mm=_clamp(gap_y, 0.0, 50.0),
        offset_x_mm=_clamp(payload.offset_x_mm, -50.0, 50.0),
        offset_y_mm=_clamp(payload.offset_y_mm, -50.0, 50.0),
        back_offset_x_mm=_clamp(payload.back_offset_x_mm, -50.0, 50.0),
        back_offset_y_mm=_clamp(payload.back_offset_y_mm, -50.0, 50.0),
        bleed_enabled=payload.bleed_enabled,
        bleed_mm=_clamp(payload.bleed_mm, 0.0, 10.0),
        card_guides_enabled=card_guides_enabled,
        card_guides_style=_one_of(card_style, ("corners", "full"), "corners"),
        card_guides_shape=_one_of(payload.card_guides_shape, ("square", "round"), "square"),
        card_guides_pattern=_one_of(card_pattern, ("solid", "dashed", "dotted"), "solid"),
        card_guides_placement=_one_of(card_placement, ("outside", "middle", "inside"), "outside"),
        card_guides_length_mm=_clamp(card_length, 0.5, 30.0),
        card_guides_color=card_color if card_color.startswith("#") else "#606060",
        card_guides_width_pt=_clamp(card_width, 0.1, 3.0),
        page_guides=_one_of(payload.page_guides, ("none", "full_lines", "corners_only"), "none"),
        hide_card_guides_front=payload.hide_card_guides_front,
        hide_card_guides_back=payload.hide_card_guides_back,
        hide_page_guides_front=payload.hide_page_guides_front,
        hide_page_guides_back=payload.hide_page_guides_back,
        reg_marks_enabled=payload.reg_marks_enabled,
        reg_marks_inset_mm=_clamp(payload.reg_marks_inset_mm, 0.0, 50.0),
        reg_marks_size_mm=_clamp(payload.reg_marks_size_mm, 1.0, 20.0),
        include_backs=payload.include_backs,
        backs_layout=_one_of(payload.backs_layout, ("append", "duplex"), "append"),
        backs_content=_one_of(payload.backs_content, ("dfc_only", "all_cards"), "all_cards"),
        backs_compact_fill=payload.backs_compact_fill,
        page_range=payload.page_range[:200],
        show_footer=payload.show_footer,
    )


@router.get("/decks/{deck_id}/build-progress", response_model=BuildProgressResponse)
async def get_build_progress(deck_id: int) -> BuildProgressResponse:
    progress = build_progress.get(deck_id)
    if progress is None:
        return BuildProgressResponse(
            active=False,
            deck_id=deck_id,
            total=0,
            current=0,
            current_name="",
            kind="xml",
            done=True,
            error=None,
            elapsed_seconds=0.0,
            eta_seconds=None,
            percent=0.0,
        )
    return BuildProgressResponse(active=True, **progress.to_dict())


@router.post("/decks/{deck_id}/build-pdf", response_model=PDFBuildResponse)
async def build_pdf_endpoint(
    deck_id: int,
    payload: BuildPDFRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    art_cache: ArtCacheDep,
) -> PDFBuildResponse:
    deck = await get_deck_or_404(db, deck_id)
    resolved = await resolve_with_progress(db, scryfall, art_cache, deck, kind="pdf")
    out_path = export_path(deck.name, ".pdf")
    options = _pdf_options(payload)
    cardback = await resolve_deck_cardback(db, deck)

    result = await asyncio.to_thread(
        build_pdf, resolved, out_path, options, cardback_path_override=cardback
    )
    build_progress.finish(deck_id)
    filename = Path(str(result.pdf_path)).name
    await deck_activity.log_event(
        db,
        deck_id,
        K.PDF_GENERATED,
        payload={
            "page_size": options.page_size,
            "orientation": options.orientation,
            "grid": f"{result.cols}x{result.rows}",
            "card_guides_enabled": options.card_guides_enabled,
            "card_guides_style": options.card_guides_style,
            "card_guides_shape": options.card_guides_shape,
            "card_guides_pattern": options.card_guides_pattern,
            "page_guides": options.page_guides,
            "bleed_enabled": options.bleed_enabled,
            "bleed_mm": options.bleed_mm,
            "include_backs": options.include_backs,
            "backs_layout": options.backs_layout,
            "backs_content": options.backs_content,
            "back_offset_x_mm": options.back_offset_x_mm,
            "back_offset_y_mm": options.back_offset_y_mm,
            "reg_marks_enabled": options.reg_marks_enabled,
            "gap_x_mm": options.gap_x_mm,
            "gap_y_mm": options.gap_y_mm,
            "page_range": options.page_range,
            "total_pages": result.total_pages,
            "total_slots": result.total_slots,
            "pdf_path": str(result.pdf_path),
            "pdf_filename": filename,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return PDFBuildResponse(
        pdf_path=str(result.pdf_path),
        filename=filename,
        total_pages=result.total_pages,
        total_slots=result.total_slots,
        cols=result.cols,
        rows=result.rows,
    )


@router.post("/decks/{deck_id}/export-images", response_model=ImagesExportResponse)
async def export_images_endpoint(
    deck_id: int,
    payload: ImagesExportRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    art_cache: ArtCacheDep,
) -> ImagesExportResponse:
    deck = await get_deck_or_404(db, deck_id)
    resolved = await resolve_with_progress(db, scryfall, art_cache, deck, kind="pdf")

    fmt = parse_decklist_format(payload.decklist_format) or "with_set"
    decklist_text = await decklist_export.build_decklist_text(
        db,
        deck_id,
        fmt=fmt,
        include_headers=True,
    )

    out_path = export_path(deck.name, "-images.zip")
    cardback = await resolve_deck_cardback(db, deck)
    result = await asyncio.to_thread(
        build_images_zip, resolved, out_path, decklist_text, cardback_path=cardback
    )
    build_progress.finish(deck_id)

    filename = out_path.name
    await deck_activity.log_event(
        db,
        deck_id,
        K.IMAGES_EXPORTED,
        payload={
            "total_files": result.total_files,
            "total_unique_cards": result.total_unique_cards,
            "total_dfc_backs": result.total_dfc_backs,
            "missing_images": result.missing_images,
            "size_bytes": result.size_bytes,
            "decklist_format": fmt,
            "zip_path": str(result.zip_path),
            "zip_filename": filename,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImagesExportResponse(
        zip_path=str(result.zip_path),
        filename=filename,
        total_files=result.total_files,
        total_unique_cards=result.total_unique_cards,
        total_dfc_backs=result.total_dfc_backs,
        included_cardback=result.included_cardback,
        missing_images=result.missing_images,
        size_bytes=result.size_bytes,
    )
