"""Endpoints de exportación: XML MPC-Autofill, estimador, historial, backup."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import DEFAULT_CARDBACK_NAME, DEFAULT_CARDSTOCK, PATHS
from mpc_forge.db import get_session
from mpc_forge.models import Deck, PrintRun
from mpc_forge.schemas import BuildXMLRequest
from mpc_forge.services import backup as backup_service
from mpc_forge.services import cost_estimator, history
from mpc_forge.services.art_cache import ArtCache
from mpc_forge.services.pdf_generator import PDFOptions, build_pdf
from mpc_forge.services.xml_generator import (
    build_xml,
    default_cardback_path,
    resolve_deck_for_xml,
)

router = APIRouter(prefix="/api", tags=["export"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def _get_art_cache(request: Request) -> ArtCache:
    return request.app.state.art_cache


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
    """El coste SIEMPRE se calcula sobre las 100 principales del mazo
    (comandante + mainboard), aunque el usuario haya importado también
    sideboard/tokens/maybeboard/companion. Es el gasto real de MPC:
    lo que va a impresión es el mazo, no lo auxiliar.
    """
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    _CORE_ROLES = {"commander", "mainboard"}
    total = sum(
        c.quantity for c in deck.cards
        if c.include and c.role in _CORE_ROLES
    )
    est = cost_estimator.estimate(total)
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
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    art_cache: Annotated[ArtCache, Depends(_get_art_cache)],
) -> XMLBuildResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    resolved = await resolve_deck_for_xml(db, scryfall, art_cache, deck)
    if not resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    cardstock = payload.cardstock or DEFAULT_CARDSTOCK
    foil = bool(payload.foil) if payload.foil is not None else False
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}.xml"

    cardback = default_cardback_path()
    result = build_xml(
        cards=resolved,
        output_path=out_path,
        cardstock=cardstock,
        foil=foil,
        cardback_path=cardback,
    )

    est = cost_estimator.estimate(result.total_cards)

    run_id: int | None = None
    if payload.create_run:
        run = await history.create_print_run_from_deck(
            db,
            deck=deck,
            cardstock=cardstock,
            foil=foil,
            tier_size=est.tier_size,
            estimated_cost_eur=est.total_eur,  # guardamos EUR total (con shipping)
            xml_path=str(result.xml_path),
            run_name=payload.run_name,
        )
        run_id = run.id

    return XMLBuildResponse(
        xml_path=str(result.xml_path),
        total_cards=result.total_cards,
        tier_size=est.tier_size,
        estimated_cost_eur=est.total_eur,  # devolvemos EUR total al frontend
        run_id=run_id,
    )


@router.get("/exports/{filename}")
async def download_export(filename: str) -> FileResponse:
    target = (PATHS.exports_dir / filename).resolve()
    if not target.exists() or PATHS.exports_dir not in target.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    # media_type se auto-detecta por extensión
    media = "application/pdf" if filename.lower().endswith(".pdf") else "application/xml"
    return FileResponse(target, media_type=media, filename=filename)


# ---- PDF imprimible ----------------------------------------------------

class BuildPDFRequest(BaseModel):
    page_size: str = "a4"           # "a4" | "letter"
    include_backs: bool = False     # incluir reversos DFC al final
    cut_marks: bool = True
    gap_mm: float = 0.0


class PDFBuildResponse(BaseModel):
    pdf_path: str
    total_pages: int
    total_slots: int


@router.post("/decks/{deck_id}/build-pdf", response_model=PDFBuildResponse)
async def build_pdf_endpoint(
    deck_id: int,
    payload: BuildPDFRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    art_cache: Annotated[ArtCache, Depends(_get_art_cache)],
) -> PDFBuildResponse:
    """Genera un PDF listo para imprimir (3×3 cartas por A4, tamaño real MTG).

    Reutiliza el mismo pipeline de resolución que el XML: descarga las imágenes
    que aún no estén cacheadas, respeta las elecciones de arte (custom u oficial),
    y produce un PDF con la máxima calidad posible (imágenes sin recomprimir).
    """
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    resolved = await resolve_deck_for_xml(db, scryfall, art_cache, deck)
    if not resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}.pdf"
    options = PDFOptions(
        page_size="letter" if payload.page_size.lower() == "letter" else "a4",
        include_backs=payload.include_backs,
        cut_marks=payload.cut_marks,
        gap_mm=payload.gap_mm,
    )
    result = build_pdf(resolved, out_path, options)
    return PDFBuildResponse(
        pdf_path=str(result.pdf_path),
        total_pages=result.total_pages,
        total_slots=result.total_slots,
    )


# --- Historial ---

class PrintRunView(BaseModel):
    id: int
    name: str
    created_at: datetime
    cardstock: str
    total_cards: int
    tier_size: int
    estimated_cost_eur: float
    xml_path: str | None
    item_count: int


@router.get("/runs", response_model=list[PrintRunView])
async def list_runs(db: DbDep) -> list[PrintRunView]:
    runs = (
        await db.scalars(
            select(PrintRun).options(selectinload(PrintRun.items)).order_by(PrintRun.created_at.desc())
        )
    ).all()
    return [
        PrintRunView(
            id=r.id,
            name=r.name,
            created_at=r.created_at,
            cardstock=r.cardstock,
            total_cards=r.total_cards,
            tier_size=r.tier_size,
            estimated_cost_eur=r.estimated_cost_eur,
            xml_path=r.xml_path,
            item_count=len(r.items),
        )
        for r in runs
    ]


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: int, db: DbDep) -> None:
    run = await db.get(PrintRun, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await db.delete(run)
    await db.commit()


# --- Backup ---

class BackupResponse(BaseModel):
    path: str
    size_bytes: int


@router.post("/backup", response_model=BackupResponse)
async def create_backup_endpoint() -> BackupResponse:
    zip_path = backup_service.create_backup()
    return BackupResponse(path=str(zip_path), size_bytes=zip_path.stat().st_size)
