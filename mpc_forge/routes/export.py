"""Endpoints de exportación: XML MPC-Autofill, estimador, historial, backup."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import DEFAULT_CARDBACK_NAME, DEFAULT_CARDSTOCK, PATHS
from mpc_forge.db import get_session
from mpc_forge.models import Deck, DeckCard, PrintRun
from mpc_forge.schemas import BuildXMLRequest
from mpc_forge.services import backup as backup_service
from mpc_forge.services import build_progress, cost_estimator, deck_activity, decklist_export, history
from mpc_forge.services.art_cache import ArtCache
from mpc_forge.services.deck_activity import DeckActivityKind as K
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


async def _resolve_deck_cardback(db: AsyncSession, deck: Deck) -> Path | None:
    """Devuelve la ruta al cardback que debe usarse para este mazo.
    Prioridad: deck.custom_cardback_art_id → default_cardback_path() global.
    Si el CustomArt referenciado no existe en disco, cae al global."""
    from mpc_forge.models import CustomArt

    if deck.custom_cardback_art_id is not None:
        art = await db.get(CustomArt, deck.custom_cardback_art_id)
        if art is not None:
            candidate = PATHS.custom_art_dir / art.relative_path
            if candidate.exists():
                return candidate
    return default_cardback_path()


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

    # Contamos las cartas que va a procesar el resolver para inicializar el
    # tracker. Es una query barata (index sobre deck_id + role).
    total_to_resolve = (
        await db.scalar(
            select(func.count(DeckCard.id)).where(
                DeckCard.deck_id == deck_id, DeckCard.include.is_(True)
            )
        )
    ) or 0
    build_progress.start(deck_id, total_to_resolve, kind="xml")

    try:
        resolved = await resolve_deck_for_xml(
            db, scryfall, art_cache, deck,
            on_progress=lambda name: build_progress.tick(deck_id, name),
        )
    except Exception as e:  # noqa: BLE001
        build_progress.finish(deck_id, error=str(e))
        raise
    if not resolved:
        build_progress.finish(deck_id, error="Mazo sin cartas resueltas")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    cardstock = payload.cardstock or DEFAULT_CARDSTOCK
    foil = bool(payload.foil) if payload.foil is not None else False
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}.xml"

    cardback = default_cardback_path()
    result = await asyncio.to_thread(
        build_xml,
        cards=resolved,
        output_path=out_path,
        cardstock=cardstock,
        foil=foil,
        cardback_path=cardback,
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
            estimated_cost_eur=est.total_eur,  # guardamos EUR total (con shipping)
            xml_path=str(result.xml_path),
            run_name=payload.run_name,
        )
        run_id = run.id

    # Timeline: dejamos huella del XML generado con las opciones para que
    # el usuario pueda ver más adelante qué configuración usó cada vez.
    await deck_activity.log_event(
        db, deck_id, K.XML_GENERATED,
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
        estimated_cost_eur=est.total_eur,  # devolvemos EUR total al frontend
        run_id=run_id,
    )


# ---- Split de print runs para mazos grandes (Fase 3 · T10) -----------------


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


@router.get(
    "/decks/{deck_id}/print-runs/preview",
    response_model=PrintRunSplitResponse,
)
async def preview_print_runs(
    deck_id: int,
    db: DbDep,
    max_tier: int | None = None,
    optimize: bool = False,
) -> PrintRunSplitResponse:
    """Previsualiza cómo se partiría el mazo en print runs MPC.

    ``max_tier`` opcional para forzar un techo (ej. 108 para dividir aunque
    quepan en 612). Por defecto usa el tier máximo definido en `MPC_TIERS`.

    ``optimize`` (Extras · F3/T10): si True, usa el DP solver
    (`split_into_runs_optimized`) para minimizar wasted_slots.

    NO descarga arte ni genera XMLs — solo cuenta slots vía
    ``plan_deck_slots`` que consulta la BD. Útil para que la UI muestre
    "tu mazo son 700 cartas → 2 runs (612 + 88)" al instante.
    """
    from mpc_forge.services.print_runs import (
        split_into_runs, split_into_runs_optimized, summary_dict,
    )
    from mpc_forge.services.xml_generator import plan_deck_slots

    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    slots = await plan_deck_slots(db, deck)
    if not slots:
        return PrintRunSplitResponse(
            total_runs=0, total_cards=0, total_wasted_slots=0,
            total_subtotal_usd=0.0, runs=[],
        )
    if optimize:
        plan = split_into_runs_optimized(slots)
    else:
        plan = split_into_runs(slots, max_tier=max_tier)
    return PrintRunSplitResponse(**summary_dict(plan))


class BuildSplitXMLRequest(BaseModel):
    """Payload para generar N XMLs de una vez (uno por run)."""
    cardstock: str | None = None
    foil: bool = False
    max_tier: int | None = None
    create_runs: bool = True
    """Si True, cada XML genera además su print_run en historial."""
    web_mode: bool = False
    """Si True, el XML generado es compatible con mpcfill.com (``<id>`` vacío)."""


class BuildSplitXMLResponse(BaseModel):
    total_runs: int
    xml_paths: list[str]
    total_cards: int
    total_subtotal_usd: float
    run_ids: list[int]


@router.post(
    "/decks/{deck_id}/build-split-xml",
    response_model=BuildSplitXMLResponse,
)
async def build_split_xml_endpoint(
    deck_id: int,
    payload: BuildSplitXMLRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    art_cache: Annotated[ArtCache, Depends(_get_art_cache)],
) -> BuildSplitXMLResponse:
    """Genera un XML por cada print run tras dividir el mazo.

    Cada fichero comparte el mismo cardback (custom o default) y cardstock.
    Se numeran ``-run1of3.xml``, ``-run2of3.xml``, etc.
    """
    from mpc_forge.services.print_runs import split_into_runs

    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    resolved = await resolve_deck_for_xml(db, scryfall, art_cache, deck)
    if not resolved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    plan = split_into_runs(resolved, max_tier=payload.max_tier)
    if not plan.runs:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El split no produjo runs")

    cardstock = payload.cardstock or DEFAULT_CARDSTOCK
    foil = payload.foil
    cardback = await _resolve_deck_cardback(db, deck)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    xml_paths: list[str] = []
    run_ids: list[int] = []
    for run_plan in plan.runs:
        suffix = f"-run{run_plan.run_index + 1}of{plan.total_runs}"
        out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}{suffix}.xml"
        r = await asyncio.to_thread(
            build_xml,
            cards=run_plan.cards, output_path=out_path,
            cardstock=cardstock, foil=foil, cardback_path=cardback,
            web_mode=payload.web_mode,
        )
        xml_paths.append(str(r.xml_path))

        if payload.create_runs:
            run_row = await history.create_print_run_from_deck(
                db, deck=deck, cardstock=cardstock, foil=foil,
                tier_size=run_plan.tier_size,
                estimated_cost_eur=run_plan.subtotal_usd * cfg.USD_TO_EUR,
                xml_path=str(r.xml_path),
                run_name=f"{deck.name} · run {run_plan.run_index + 1}/{plan.total_runs}",
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


# ---- Decklist como texto plano ---------------------------------------------
# Copiar la lista serializada al portapapeles o descargarla como .txt para
# reimportarla en MTGPrint, MPCFill, Moxfield, MTGA, etc.

class DecklistResponse(BaseModel):
    text: str
    format: str
    total_cards: int
    filename: str


@router.get("/decks/{deck_id}/decklist", response_model=DecklistResponse)
async def get_decklist(
    deck_id: int,
    db: DbDep,
    format: str = "with_set",  # "simple" | "with_set" | "arena"
    include_headers: bool = True,
) -> DecklistResponse:
    """Devuelve el mazo serializado como texto plano.

    Solo cuenta cartas con include=True. Los tokens y meld_result se omiten
    porque no forman parte de la lista importable.
    """
    if format not in {"simple", "with_set", "arena"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Formato desconocido: {format!r}")

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    text = await decklist_export.build_decklist_text(
        db, deck_id, fmt=format, include_headers=include_headers,  # type: ignore[arg-type]
    )
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El mazo no tiene cartas activas")

    total = sum(1 for line in text.splitlines() if line and line[0].isdigit())
    return DecklistResponse(
        text=text,
        format=format,
        total_cards=total,
        filename=decklist_export.filename_for(deck.name, format),  # type: ignore[arg-type]
    )


@router.get("/decks/{deck_id}/decklist.txt")
async def download_decklist(
    deck_id: int,
    db: DbDep,
    format: str = "with_set",
    include_headers: bool = True,
) -> FileResponse:
    """Descarga el mazo como fichero .txt (para guardar/enviar)."""
    if format not in {"simple", "with_set", "arena"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Formato desconocido: {format!r}")

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    text = await decklist_export.build_decklist_text(
        db, deck_id, fmt=format, include_headers=include_headers,  # type: ignore[arg-type]
    )
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El mazo no tiene cartas activas")

    filename = decklist_export.filename_for(deck.name, format)  # type: ignore[arg-type]
    out_path = PATHS.exports_dir / filename
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(out_path, media_type="text/plain; charset=utf-8", filename=filename)


@router.get("/exports/{filename}")
async def download_export(filename: str) -> FileResponse:
    target = (PATHS.exports_dir / filename).resolve()
    if not target.exists() or PATHS.exports_dir not in target.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    # media_type se auto-detecta por extensión
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    media = {
        "pdf": "application/pdf",
        "zip": "application/zip",
        "txt": "text/plain; charset=utf-8",
        "xml": "application/xml",
    }.get(ext, "application/octet-stream")
    return FileResponse(target, media_type=media, filename=filename)


@router.api_route("/cardback", methods=["GET", "HEAD"])
async def get_default_cardback() -> FileResponse:
    """Sirve el cardback estándar para que el preview del PDF Studio pueda
    pintarlo en las páginas de reversos cuando el modo es 'all_cards'."""
    path = default_cardback_path()
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sin cardback configurado")
    ext = path.suffix.lower().lstrip(".")
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    return FileResponse(path, media_type=media)


# ---- Cardback específico por mazo (v2 PDF Studio) --------------------------

class DeckCardbackSettings(BaseModel):
    """Estado actual del cardback del mazo. Cuando `custom_art_id` es None,
    se usa el `default_cardback_path()` global."""
    deck_id: int
    using_custom: bool
    custom_art_id: int | None = None
    filename: str | None = None
    variant_label: str | None = None
    image_url: str | None = None
    default_image_url: str | None = None  # URL del cardback global (si existe)


class SetDeckCardbackRequest(BaseModel):
    # None → revertir al default global. Un id válido → usar ese CustomArt.
    custom_art_id: int | None = None


async def _load_deck_cardback_settings(
    db: AsyncSession, deck_id: int,
) -> DeckCardbackSettings:
    from mpc_forge.models import CustomArt
    from mpc_forge.services import custom_art as custom_art_service

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    default_url = "/api/cardback" if default_cardback_path() is not None else None

    if deck.custom_cardback_art_id is None:
        return DeckCardbackSettings(
            deck_id=deck_id, using_custom=False,
            default_image_url=default_url,
        )

    art = await db.get(CustomArt, deck.custom_cardback_art_id)
    if art is None:
        # FK huérfana (el CustomArt fue borrado). Auto-corregimos.
        deck.custom_cardback_art_id = None
        await db.commit()
        return DeckCardbackSettings(
            deck_id=deck_id, using_custom=False,
            default_image_url=default_url,
        )

    return DeckCardbackSettings(
        deck_id=deck_id,
        using_custom=True,
        custom_art_id=art.id,
        filename=art.filename,
        variant_label=art.variant_label,
        image_url=custom_art_service.custom_art_url(art.relative_path),
        default_image_url=default_url,
    )


@router.get("/decks/{deck_id}/cardback-settings", response_model=DeckCardbackSettings)
async def get_deck_cardback(deck_id: int, db: DbDep) -> DeckCardbackSettings:
    return await _load_deck_cardback_settings(db, deck_id)


@router.put("/decks/{deck_id}/cardback-settings", response_model=DeckCardbackSettings)
async def set_deck_cardback(
    deck_id: int, payload: SetDeckCardbackRequest, db: DbDep,
) -> DeckCardbackSettings:
    from mpc_forge.models import CustomArt

    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    if payload.custom_art_id is not None:
        art = await db.get(CustomArt, payload.custom_art_id)
        if art is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "CustomArt no encontrado")
        deck.custom_cardback_art_id = art.id
    else:
        deck.custom_cardback_art_id = None

    await db.commit()
    return await _load_deck_cardback_settings(db, deck_id)


@router.delete("/decks/{deck_id}/cardback-settings", response_model=DeckCardbackSettings)
async def clear_deck_cardback(deck_id: int, db: DbDep) -> DeckCardbackSettings:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")
    deck.custom_cardback_art_id = None
    await db.commit()
    return await _load_deck_cardback_settings(db, deck_id)


# ---- PDF imprimible ----------------------------------------------------

class BuildPDFRequest(BaseModel):
    """Payload del PDF Studio v2. Todos los campos son opcionales — si no
    vienen, caen a los defaults del dataclass ``PDFOptions``.

    Legacy: los campos ``cut_marks``, ``gap_mm``, ``guides_enabled``,
    ``guides_style``, ``guides_stroke``, ``guides_placement``,
    ``guides_length_mm``, ``guides_color``, ``guides_width_pt`` se aceptan
    para no romper integraciones anteriores; se traducen internamente al
    modelo nuevo (``card_guides_*``).
    """
    # Página
    page_size: str = "a4"                 # "a4" | "letter" | "a3"
    orientation: str = "portrait"         # "portrait" | "landscape"
    cols: int = 3
    rows: int = 3

    # Espaciado
    gap_x_mm: float = 0.0
    gap_y_mm: float = 0.0

    # Offsets
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    back_offset_x_mm: float = 0.0
    back_offset_y_mm: float = 0.0

    # Bleed
    bleed_enabled: bool = False
    bleed_mm: float = 0.0

    # Card guides
    card_guides_enabled: bool = True
    card_guides_style: str = "corners"     # "corners" | "full"
    card_guides_shape: str = "square"      # "square"  | "round"
    card_guides_pattern: str = "solid"     # "solid"   | "dashed" | "dotted"
    card_guides_placement: str = "outside" # "outside" | "middle" | "inside"
    card_guides_length_mm: float = 4.0
    card_guides_color: str = "#606060"
    card_guides_width_pt: float = 0.4

    # Page guides
    page_guides: str = "none"              # "none" | "full_lines" | "corners_only"

    # Duplex hide flags
    hide_card_guides_front: bool = False
    hide_card_guides_back: bool = False
    hide_page_guides_front: bool = False
    hide_page_guides_back: bool = False

    # Marcas de registro (Silhouette / Cricut)
    reg_marks_enabled: bool = False
    reg_marks_inset_mm: float = 10.0
    reg_marks_size_mm: float = 5.0

    # Contenido
    include_backs: bool = False
    backs_layout: str = "append"           # "append" | "duplex"
    backs_content: str = "all_cards"       # "all_cards" | "dfc_only"
    backs_compact_fill: bool = True        # aprovecha huecos de la última hoja de fronts

    # Rango de páginas
    page_range: str = ""

    # Pie
    show_footer: bool = True

    # ---- Legacy (traducidos si vienen) ----
    cut_marks: bool | None = None          # → card_guides_enabled
    gap_mm: float | None = None            # → gap_x_mm & gap_y_mm
    guides_enabled: bool | None = None     # → card_guides_enabled
    guides_style: str | None = None        # → card_guides_style
    guides_stroke: str | None = None       # → card_guides_pattern
    guides_placement: str | None = None    # → card_guides_placement
    guides_length_mm: float | None = None  # → card_guides_length_mm
    guides_color: str | None = None        # → card_guides_color
    guides_width_pt: float | None = None   # → card_guides_width_pt


class PDFBuildResponse(BaseModel):
    pdf_path: str
    filename: str
    total_pages: int
    total_slots: int
    cols: int
    rows: int


class ImagesExportRequest(BaseModel):
    """Sin campos por ahora — el ZIP incluye siempre las imágenes únicas del
    mazo + decklist.txt + README.txt. Reservado para el futuro por si
    queremos permitir escoger formato de decklist, incluir tokens, etc."""
    decklist_format: str = "with_set"      # "simple" | "with_set" | "arena"


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
    kind: str  # "xml" | "pdf"
    done: bool
    error: str | None
    elapsed_seconds: float
    eta_seconds: float | None
    percent: float


@router.get("/decks/{deck_id}/build-progress", response_model=BuildProgressResponse)
async def get_build_progress(deck_id: int) -> BuildProgressResponse:
    """Estado del build XML/PDF en curso (o del último terminado, si el
    frontend aún no lo ha limpiado).

    El frontend hace polling a este endpoint cada ~300ms mientras el POST
    /build-xml o /build-pdf está pendiente, para pintar una barra real de
    "42/100 · Sol Ring…". Cuando ``done=true`` deja de hacer polling.

    Si no hay build activo devuelve ``active=false`` (nunca 404 — es un
    estado válido y evita ruido en la consola del navegador).
    """
    p = build_progress.get(deck_id)
    if p is None:
        return BuildProgressResponse(
            active=False, deck_id=deck_id, total=0, current=0, current_name="",
            kind="xml", done=True, error=None, elapsed_seconds=0.0,
            eta_seconds=None, percent=0.0,
        )
    d = p.to_dict()
    return BuildProgressResponse(active=True, **d)


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

    total_to_resolve = (
        await db.scalar(
            select(func.count(DeckCard.id)).where(
                DeckCard.deck_id == deck_id, DeckCard.include.is_(True)
            )
        )
    ) or 0
    build_progress.start(deck_id, total_to_resolve, kind="pdf")

    try:
        resolved = await resolve_deck_for_xml(
            db, scryfall, art_cache, deck,
            on_progress=lambda name: build_progress.tick(deck_id, name),
        )
    except Exception as e:  # noqa: BLE001
        build_progress.finish(deck_id, error=str(e))
        raise
    if not resolved:
        build_progress.finish(deck_id, error="Mazo sin cartas resueltas")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}.pdf"

    # --- Traducción legacy → nuevo modelo ---
    def _one_of(value: str, allowed: tuple[str, ...], default: str) -> str:
        v = (value or "").lower().strip()
        return v if v in allowed else default

    # cut_marks / guides_enabled → card_guides_enabled
    card_guides_enabled = payload.card_guides_enabled
    if payload.cut_marks is not None:
        card_guides_enabled = payload.cut_marks
    if payload.guides_enabled is not None:
        card_guides_enabled = payload.guides_enabled

    # gap_mm → gap_x/y
    if payload.gap_mm is not None:
        gap_x = gap_y = payload.gap_mm
    else:
        gap_x, gap_y = payload.gap_x_mm, payload.gap_y_mm

    # Legacy guides_* → card_guides_*
    card_style = payload.guides_style if payload.guides_style is not None else payload.card_guides_style
    card_pattern = payload.guides_stroke if payload.guides_stroke is not None else payload.card_guides_pattern
    card_placement = payload.guides_placement if payload.guides_placement is not None else payload.card_guides_placement
    card_length = payload.guides_length_mm if payload.guides_length_mm is not None else payload.card_guides_length_mm
    card_color = payload.guides_color if payload.guides_color is not None else payload.card_guides_color
    card_width = payload.guides_width_pt if payload.guides_width_pt is not None else payload.card_guides_width_pt

    options = PDFOptions(
        page_size=_one_of(payload.page_size, ("a4", "letter", "a3"), "a4"),  # type: ignore[arg-type]
        orientation=_one_of(payload.orientation, ("portrait", "landscape"), "portrait"),  # type: ignore[arg-type]
        cols=max(0, min(20, payload.cols)),
        rows=max(0, min(20, payload.rows)),
        gap_x_mm=max(0.0, min(50.0, gap_x)),
        gap_y_mm=max(0.0, min(50.0, gap_y)),
        offset_x_mm=max(-50.0, min(50.0, payload.offset_x_mm)),
        offset_y_mm=max(-50.0, min(50.0, payload.offset_y_mm)),
        back_offset_x_mm=max(-50.0, min(50.0, payload.back_offset_x_mm)),
        back_offset_y_mm=max(-50.0, min(50.0, payload.back_offset_y_mm)),
        bleed_enabled=payload.bleed_enabled,
        bleed_mm=max(0.0, min(10.0, payload.bleed_mm)),
        card_guides_enabled=card_guides_enabled,
        card_guides_style=_one_of(card_style, ("corners", "full"), "corners"),  # type: ignore[arg-type]
        card_guides_shape=_one_of(payload.card_guides_shape, ("square", "round"), "square"),  # type: ignore[arg-type]
        card_guides_pattern=_one_of(card_pattern, ("solid", "dashed", "dotted"), "solid"),  # type: ignore[arg-type]
        card_guides_placement=_one_of(card_placement, ("outside", "middle", "inside"), "outside"),  # type: ignore[arg-type]
        card_guides_length_mm=max(0.5, min(30.0, card_length)),
        card_guides_color=card_color if card_color.startswith("#") else "#606060",
        card_guides_width_pt=max(0.1, min(3.0, card_width)),
        page_guides=_one_of(payload.page_guides, ("none", "full_lines", "corners_only"), "none"),  # type: ignore[arg-type]
        hide_card_guides_front=payload.hide_card_guides_front,
        hide_card_guides_back=payload.hide_card_guides_back,
        hide_page_guides_front=payload.hide_page_guides_front,
        hide_page_guides_back=payload.hide_page_guides_back,
        reg_marks_enabled=payload.reg_marks_enabled,
        reg_marks_inset_mm=max(0.0, min(50.0, payload.reg_marks_inset_mm)),
        reg_marks_size_mm=max(1.0, min(20.0, payload.reg_marks_size_mm)),
        include_backs=payload.include_backs,
        backs_layout=_one_of(payload.backs_layout, ("append", "duplex"), "append"),  # type: ignore[arg-type]
        backs_content=_one_of(payload.backs_content, ("dfc_only", "all_cards"), "all_cards"),  # type: ignore[arg-type]
        backs_compact_fill=payload.backs_compact_fill,
        page_range=payload.page_range[:200],
        show_footer=payload.show_footer,
    )

    # Cardback específico del mazo (v2). El generador solo lo usa cuando
    # backs_content='all_cards'; para el resto no consulta el disco.
    cardback = await _resolve_deck_cardback(db, deck)

    result = await asyncio.to_thread(
        build_pdf, resolved, out_path, options, cardback_path_override=cardback
    )
    build_progress.finish(deck_id)
    filename = Path(str(result.pdf_path)).name
    await deck_activity.log_event(
        db, deck_id, K.PDF_GENERATED,
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
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    art_cache: Annotated[ArtCache, Depends(_get_art_cache)],
) -> ImagesExportResponse:
    """Genera un ZIP con las imágenes de cada carta única + decklist.txt +
    README.txt. Reutiliza el mismo pipeline de resolución que XML/PDF."""
    from mpc_forge.services.image_export import build_images_zip

    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    total_to_resolve = (
        await db.scalar(
            select(func.count(DeckCard.id)).where(
                DeckCard.deck_id == deck_id, DeckCard.include.is_(True)
            )
        )
    ) or 0
    build_progress.start(deck_id, total_to_resolve, kind="pdf")  # UI ya sabe pintar "pdf"

    try:
        resolved = await resolve_deck_for_xml(
            db, scryfall, art_cache, deck,
            on_progress=lambda name: build_progress.tick(deck_id, name),
        )
    except Exception as e:  # noqa: BLE001
        build_progress.finish(deck_id, error=str(e))
        raise
    if not resolved:
        build_progress.finish(deck_id, error="Mazo sin cartas resueltas")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Mazo sin cartas resueltas")

    # Decklist en el formato que pida el usuario.
    fmt = payload.decklist_format if payload.decklist_format in {"simple", "with_set", "arena"} else "with_set"
    decklist_text = await decklist_export.build_decklist_text(
        db, deck_id, fmt=fmt, include_headers=True,  # type: ignore[arg-type]
    )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = PATHS.exports_dir / f"{slugify(deck.name)}-{stamp}-images.zip"
    cardback = await _resolve_deck_cardback(db, deck)
    result = await asyncio.to_thread(
        build_images_zip, resolved, out_path, decklist_text, cardback_path=cardback
    )
    build_progress.finish(deck_id)

    filename = out_path.name
    await deck_activity.log_event(
        db, deck_id, K.IMAGES_EXPORTED,
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
