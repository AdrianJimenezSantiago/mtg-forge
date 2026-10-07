from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import Deck
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.routes.export._shared import (
    DecklistFormat,
    get_deck_or_404,
    make_router,
    parse_decklist_format,
)
from mpc_forge.services.decks import decklist_export

router = make_router()


class DecklistResponse(BaseModel):
    text: str
    format: str
    total_cards: int
    filename: str


async def _build_text(
    db: AsyncSession, deck_id: int, fmt: str, include_headers: bool
) -> tuple[Deck, str, DecklistFormat]:
    parsed = parse_decklist_format(fmt)
    if parsed is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Formato desconocido: {fmt!r}")
    deck = await get_deck_or_404(db, deck_id)
    text = await decklist_export.build_decklist_text(
        db,
        deck_id,
        fmt=parsed,
        include_headers=include_headers,
    )
    if not text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El mazo no tiene cartas activas")
    return deck, text, parsed


@router.get("/decks/{deck_id}/decklist", response_model=DecklistResponse)
async def get_decklist(
    deck_id: int,
    db: DbDep,
    format: str = "with_set",
    include_headers: bool = True,
) -> DecklistResponse:
    deck, text, parsed = await _build_text(db, deck_id, format, include_headers)
    total = sum(1 for line in text.splitlines() if line and line[0].isdigit())
    return DecklistResponse(
        text=text,
        format=format,
        total_cards=total,
        filename=decklist_export.filename_for(deck.name, parsed),
    )


@router.get("/decks/{deck_id}/decklist.txt")
async def download_decklist(
    deck_id: int,
    db: DbDep,
    format: str = "with_set",
    include_headers: bool = True,
) -> FileResponse:
    deck, text, parsed = await _build_text(db, deck_id, format, include_headers)
    filename = decklist_export.filename_for(deck.name, parsed)
    out_path = cfg.PATHS.exports_dir / filename
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(out_path, media_type="text/plain; charset=utf-8", filename=filename)
