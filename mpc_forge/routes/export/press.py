from __future__ import annotations

import asyncio
from collections import Counter
from http import HTTPStatus
from pathlib import Path

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from mpc_forge.models import DeckCard
from mpc_forge.routes.dependencies import ArtCacheDep, DbDep, ScryfallDep
from mpc_forge.routes.export._shared import (
    export_path,
    get_deck_or_404,
    make_router,
    resolve_with_progress,
)
from mpc_forge.routes.export.cardbacks import resolve_deck_cardback
from mpc_forge.services.decks import deck_activity
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K
from mpc_forge.services.printing import build_progress
from mpc_forge.services.printing.press_color import PressColorError
from mpc_forge.services.printing.press_export import (
    PressExportError,
    build_press_package,
    build_press_pdf,
    build_press_slots,
)
from mpc_forge.services.printing.press_image import PressImageError
from mpc_forge.services.printing.print_shops import PRINT_SHOPS, PrintShopSpec, get_print_shop
from mpc_forge.services.printing.xml_generator import DeckCardResolved

router = make_router()


class PrintShopView(BaseModel):
    key: str
    shop_name: str
    product_name: str
    trim_w_mm: float
    trim_h_mm: float
    bleed_mm: float
    safe_margin_mm: float
    corner_radius_mm: float
    page_w_mm: float
    page_h_mm: float
    ppi: int
    max_bytes_per_page: int
    color_profile: str
    package_entry: str
    delivery_email: str
    delivery_url: str
    templates_url: str


class PressExportRequest(BaseModel):
    shop: str = "pinbro_poker"


class PressWarningView(BaseModel):
    card_name: str
    face: str
    code: str


class PressExportResponse(BaseModel):
    shop: PrintShopView
    zip_filename: str
    pdf_filename: str
    total_cards: int
    total_pages: int
    unique_images: int
    dfc_backs: int
    cardback_backs: int
    generated_bleed_images: int
    pdf_size_bytes: int
    zip_size_bytes: int
    largest_page_bytes: int
    min_source_ppi: int
    warnings: list[PressWarningView]
    delivery_message: str


def _shop_or_400(key: str) -> PrintShopSpec:
    spec = get_print_shop(key)
    if spec is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "unknown_shop", "message": f"Imprenta desconocida: {key}"},
        )
    return spec


def _missing_cards(expected: Counter[str], resolved: list[DeckCardResolved]) -> list[str]:
    got = Counter[str]()
    for c in resolved:
        got[c.name] += c.quantity
    return sorted(name for name, qty in expected.items() if got[name] < qty)


@router.get("/print-shops", response_model=list[PrintShopView])
async def list_print_shops() -> list[PrintShopView]:
    return [PrintShopView(**spec.to_dict()) for spec in PRINT_SHOPS.values()]


@router.post("/decks/{deck_id}/export-press", response_model=PressExportResponse)
async def export_press_endpoint(
    deck_id: int,
    payload: PressExportRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    art_cache: ArtCacheDep,
) -> PressExportResponse:
    spec = _shop_or_400(payload.shop)
    deck = await get_deck_or_404(db, deck_id)

    expected = Counter[str]()
    for name, qty in (
        await db.execute(
            select(DeckCard.name, DeckCard.quantity).where(
                DeckCard.deck_id == deck_id, DeckCard.include.is_(True)
            )
        )
    ).all():
        expected[name] += qty

    resolved = await resolve_with_progress(db, scryfall, art_cache, deck, kind="pdf")
    try:
        missing = _missing_cards(expected, resolved)
        if missing:
            raise PressExportError(
                "missing_images",
                "Hay cartas sin imagen; elige un arte para cada una antes de exportar.",
                missing,
            )
        cardback = await resolve_deck_cardback(db, deck)
        pairs = build_press_slots(resolved, cardback)

        unique_images = len({s.path for pair in pairs for s in pair})
        build_progress.start(deck_id, unique_images, kind="press")
        stem = export_path(f"{spec.key}-{deck.name}", "")
        pdf_path = stem.with_suffix(".pdf")
        zip_path = stem.with_suffix(".zip")
        result = await asyncio.to_thread(
            build_press_pdf,
            pairs,
            pdf_path,
            spec,
            title=f"{deck.name} · {spec.package_pdf_name}",
            on_image=lambda name: build_progress.tick(deck_id, name),
        )
        zip_size = await asyncio.to_thread(build_press_package, pdf_path, zip_path, spec)
    except PressExportError as e:
        build_progress.finish(deck_id, error=str(e))
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"code": e.code, "message": str(e), "cards": e.cards},
        ) from e
    except (PressImageError, PressColorError) as e:
        build_progress.finish(deck_id, error=str(e))
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"code": "image_error", "message": str(e), "cards": []},
        ) from e
    except Exception as e:
        build_progress.finish(deck_id, error=str(e))
        raise
    build_progress.finish(deck_id)

    warnings = [
        PressWarningView(card_name=w.card_name, face=w.face, code=w.code) for w in result.warnings
    ]
    await deck_activity.log_event(
        db,
        deck_id,
        K.PRESS_EXPORTED,
        payload={
            "shop": spec.key,
            "shop_name": spec.shop_name,
            "product_name": spec.product_name,
            "total_cards": result.total_cards,
            "total_pages": result.total_pages,
            "pdf_size_bytes": result.pdf_size_bytes,
            "zip_size_bytes": zip_size,
            "warnings": len(warnings),
            "pdf_filename": Path(result.pdf_path).name,
            "zip_filename": zip_path.name,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return PressExportResponse(
        shop=PrintShopView(**spec.to_dict()),
        zip_filename=zip_path.name,
        pdf_filename=Path(result.pdf_path).name,
        total_cards=result.total_cards,
        total_pages=result.total_pages,
        unique_images=result.unique_images,
        dfc_backs=result.dfc_backs,
        cardback_backs=result.cardback_backs,
        generated_bleed_images=result.generated_bleed_images,
        pdf_size_bytes=result.pdf_size_bytes,
        zip_size_bytes=zip_size,
        largest_page_bytes=result.largest_image_bytes,
        min_source_ppi=result.min_source_ppi,
        warnings=warnings,
        delivery_message=spec.delivery_message(deck.name, result.total_cards, result.total_pages),
    )
