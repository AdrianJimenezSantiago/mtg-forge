"""Cambio de idioma de las impresiones y autocompletado de nombres.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    Depends,
    HTTPException,
    Response,
    status,
)
from pydantic import BaseModel

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import (
    Deck,
)
from mpc_forge.services import (
    deck_activity,
    deck_service,
)
from mpc_forge.services.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


from mpc_forge.routes.decks._common import (
    DbDep,
    _get_scryfall,
    make_router,
)

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
    """Diccionario code → label para poblar el selector de idiomas del frontend.

    Cache HTTP: contenido esencialmente constante. 1 hora es suficiente para
    que el navegador no pida esto en cada carga del deck editor.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return SUPPORTED_LANGS


@router.post("/{deck_id}/localize", response_model=LocalizeDeckResponse)
async def localize_deck_endpoint(
    deck_id: int,
    payload: LocalizeDeckRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> LocalizeDeckResponse:
    """Cambia todas las cartas del mazo al idioma pedido, cuando exista impresión.

    - Cartas con custom_art_front_id se saltan (respeta el arte custom del usuario).
    - Cartas ya en ese idioma no se tocan.
    - Cartas sin impresión disponible en ese idioma conservan la impresión actual
      y se listan en ``unavailable`` para que el usuario sepa cuáles siguen en su
      idioma original.

    Los printings localizados se cachean como filas independientes de
    ``PrintingCache`` (Scryfall les da su propio scryfall_id por idioma), por lo
    que llamar dos veces con el mismo idioma es prácticamente gratis la segunda
    vez.
    """
    deck = await db.get(Deck, deck_id)
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    if payload.lang not in SUPPORTED_LANGS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Idioma no soportado: {payload.lang!r}. Válidos: {sorted(SUPPORTED_LANGS)}",
        )

    result = await deck_service.localize_deck(db, scryfall, deck_id, payload.lang)
    if result["localized"] > 0 or result["unavailable"]:
        await deck_activity.log_event(
            db, deck_id, K.DECK_LOCALIZED,
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
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[str]:
    """Autocompleta nombres de cartas usando la API de Scryfall.

    Path bajo /_/ para evitar colisión con los routes de deck_id (int).
    Ante fallos de red o rate limit, devuelve lista vacía (el frontend simplemente
    no muestra sugerencias, no aparece un error molesto).
    """
    if not q or len(q.strip()) < 2:
        return []
    try:
        return await scryfall.autocomplete(q)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Autocomplete falló para %r: %s", q, e)
        return []
