from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import CustomArt, Deck
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.routes.export._shared import get_deck_or_404, make_router
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.printing.xml_generator import default_cardback_path

router = make_router()

_IMAGE_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


async def resolve_deck_cardback(db: AsyncSession, deck: Deck) -> Path | None:
    if deck.custom_cardback_art_id is not None:
        art = await db.get(CustomArt, deck.custom_cardback_art_id)
        if art is not None:
            candidate = cfg.PATHS.custom_art_dir / art.relative_path
            if candidate.exists():
                return candidate
    return default_cardback_path()


@router.api_route("/cardback", methods=["GET", "HEAD"])
async def get_default_cardback() -> FileResponse:
    path = default_cardback_path()
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sin cardback configurado")
    ext = path.suffix.lower().lstrip(".")
    return FileResponse(path, media_type=_IMAGE_MEDIA_TYPES.get(ext, "image/png"))


class DeckCardbackSettings(BaseModel):
    deck_id: int
    using_custom: bool
    custom_art_id: int | None = None
    filename: str | None = None
    variant_label: str | None = None
    image_url: str | None = None
    default_image_url: str | None = None


class SetDeckCardbackRequest(BaseModel):
    custom_art_id: int | None = None


async def _load_deck_cardback_settings(db: AsyncSession, deck_id: int) -> DeckCardbackSettings:
    deck = await get_deck_or_404(db, deck_id)
    default_url = "/api/cardback" if default_cardback_path() is not None else None
    without_custom = DeckCardbackSettings(
        deck_id=deck_id, using_custom=False, default_image_url=default_url
    )
    if deck.custom_cardback_art_id is None:
        return without_custom

    art = await db.get(CustomArt, deck.custom_cardback_art_id)
    if art is None:
        deck.custom_cardback_art_id = None
        await db.commit()
        return without_custom

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
    deck_id: int,
    payload: SetDeckCardbackRequest,
    db: DbDep,
) -> DeckCardbackSettings:
    deck = await get_deck_or_404(db, deck_id)
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
    deck = await get_deck_or_404(db, deck_id)
    deck.custom_cardback_art_id = None
    await db.commit()
    return await _load_deck_cardback_settings(db, deck_id)
