from __future__ import annotations

import logging

from fastapi import (
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel

from mpc_forge.models import Deck
from mpc_forge.routes.decks._common import make_router
from mpc_forge.routes.dependencies import DbDep, ScryfallDep
from mpc_forge.services.decks import deck_activity, localization
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


router = make_router()


SUPPORTED_LANGS: dict[str, str] = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "zhs": "简体中文",
    "zht": "繁體中文",
}


class LocalizeDeckRequest(BaseModel):
    lang: str


class LocalizeDeckResponse(BaseModel):
    lang: str
    localized: int
    unchanged: int
    unavailable: list[str]
    skipped_custom: int


@router.get("/_/supported-langs")
async def get_supported_langs(response: Response) -> dict[str, str]:
    response.headers["Cache-Control"] = "public, max-age=3600"
    return SUPPORTED_LANGS


@router.post("/{deck_id}/localize", response_model=LocalizeDeckResponse)
async def localize_deck_endpoint(
    deck_id: int,
    payload: LocalizeDeckRequest,
    db: DbDep,
    scryfall: ScryfallDep,
) -> LocalizeDeckResponse:
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    if payload.lang not in SUPPORTED_LANGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Idioma no soportado: {payload.lang!r}. Válidos: {sorted(SUPPORTED_LANGS)}",
        )

    result = await localization.localize_deck(db, scryfall, deck_id, payload.lang)
    if result["localized"] > 0 or result["unavailable"]:
        await deck_activity.log_event(
            db,
            deck_id,
            K.DECK_LOCALIZED,
            payload={
                "lang": payload.lang,
                "localized": result["localized"],
                "unchanged": result["unchanged"],
                "unavailable": result["unavailable"],
                "skipped_custom": result["skipped_custom"],
            },
            deck_name=deck.name,
        )
        await db.commit()
    return LocalizeDeckResponse(**result)


@router.get("/_/autocomplete")
async def autocomplete_card(
    q: str,
    scryfall: ScryfallDep,
) -> list[str]:
    if not q or len(q.strip()) < 2:
        return []
    try:
        return await scryfall.autocomplete(q)
    except Exception as e:
        log.warning("Autocomplete falló para %r: %s", q, e)
        return []
