"""Rutas de la UI (HTML). Todo el frontend usa Tailwind (CDN) + Alpine.js + fetch()."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mpc_forge.paths import static_dir, template_dir
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.db import get_session
from mpc_forge.models import Deck, DeckCard, PrintingCache, PrintRun

log = logging.getLogger(__name__)

TEMPLATES_DIR = template_dir()
STATIC_DIR = static_dir()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Cache-busting: los templates usan `{{ asset_v('app.js') }}` para generar URLs
# como `/static/app.js?v=1735234123`. El navegador guarda /static/app.js en cache
# con max-age=1día (nuestra CachedStaticFiles), pero cuando cambiamos el fichero
# y reiniciamos, el `v` cambia → URL nueva → cache-miss → descarga fresca.
#
# El mtime se lee UNA VEZ al arrancar la app. No hay hit por cada request. Si
# el usuario reinicia el .exe, el asset se recarga.
_ASSET_MTIMES: dict[str, int] = {}


def _asset_v(filename: str) -> int:
    """Devuelve un entero único por versión de asset (mtime del fichero).

    Cacheado en memoria: la primera llamada lee mtime del disco, las
    siguientes devuelven el valor guardado. Si el asset no existe devuelve 0
    (URL sin query — el navegador cacheará normalmente, lo cual es OK).
    """
    if filename not in _ASSET_MTIMES:
        path = STATIC_DIR / filename
        try:
            _ASSET_MTIMES[filename] = int(path.stat().st_mtime)
        except OSError:
            _ASSET_MTIMES[filename] = 0
    return _ASSET_MTIMES[filename]


# Exponemos la función a los templates como global (accesible desde cualquier
# {% extends %} o {% include %}).
templates.env.globals["asset_v"] = _asset_v

router = APIRouter(tags=["ui"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: DbDep) -> HTMLResponse:
    # Optimización: en vez de traer TODAS las cartas de TODOS los mazos solo
    # para contar el len (lo que hacía selectinload(Deck.cards)), hacemos un
    # JOIN con COUNT. Con 20 mazos × 100 cartas pasamos de traer 2000 rows
    # a solo 20 filas con el count agregado.
    result = await db.execute(
        select(Deck, func.count(DeckCard.id).label("card_count"))
        .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
        .group_by(Deck.id)
        .order_by(Deck.updated_at.desc())
    )
    deck_rows = result.all()

    # Batch: printings de los commanders para pintar la card con su arte.
    # Mismo patrón que list_decks_with_activity — WHERE ... IN (?) en una sola
    # query en lugar de N gets individuales.
    commander_ids = {d.commander_scryfall_id for d, _ in deck_rows if d.commander_scryfall_id}
    printings_by_id: dict[str, PrintingCache] = {}
    if commander_ids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(commander_ids))
            )
        ).all()
        printings_by_id = {p.scryfall_id: p for p in rows}

    decks = []
    for deck, count in deck_rows:
        deck.card_count = count  # atributo runtime, disponible en el template
        # Arte del commander (o None si no hay commander / printing sin cache).
        printing = printings_by_id.get(deck.commander_scryfall_id) if deck.commander_scryfall_id else None
        deck.commander_image_url = printing.image_normal if printing else None
        deck.commander_name = printing.name if printing else None
        decks.append(deck)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"decks": decks},
    )


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return HTMLResponse("Deck no encontrado", status_code=404)
    return templates.TemplateResponse(
        request,
        "deck.html",
        {"deck": deck},
    )


@router.get("/decks/{deck_id}/proof", response_class=HTMLResponse)
async def proof_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return HTMLResponse("Deck no encontrado", status_code=404)
    return templates.TemplateResponse(
        request,
        "proof.html",
        {"deck": deck},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    """Vista de historial. Los datos (mazos, runs, timelines) se cargan vía
    fetch desde el frontend — el template no necesita context inicial."""
    return templates.TemplateResponse(request, "history.html", {})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", {})
