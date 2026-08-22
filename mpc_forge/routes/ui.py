"""Rutas de la UI (HTML). Todo el frontend usa Tailwind (CDN) + Alpine.js + fetch()."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mpc_forge.paths import template_dir
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.db import get_session
from mpc_forge.models import Deck, DeckCard, PrintRun

TEMPLATES_DIR = template_dir()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
    decks = []
    for deck, count in result.all():
        deck.card_count = count  # atributo runtime, disponible en el template
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
async def history_page(request: Request, db: DbDep) -> HTMLResponse:
    runs = (
        await db.scalars(
            select(PrintRun).options(selectinload(PrintRun.items)).order_by(PrintRun.created_at.desc())
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "history.html",
        {"runs": runs},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", {})
