"""Rutas de la UI (HTML). Todo el frontend usa Tailwind (CDN) + Alpine.js + fetch()."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.db import get_session
from mpc_forge.models import Deck, PrintRun

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: DbDep) -> HTMLResponse:
    decks = (
        await db.scalars(
            select(Deck).options(selectinload(Deck.cards)).order_by(Deck.updated_at.desc())
        )
    ).all()
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
