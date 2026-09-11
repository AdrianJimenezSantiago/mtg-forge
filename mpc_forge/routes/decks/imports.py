"""Importación de mazos desde Moxfield, URLs de terceros y texto plano.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    Depends, HTTPException, status,
)

from mpc_forge.clients.moxfield import MoxfieldClient, MoxfieldError
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.schemas import (
    ImportFromMoxfieldRequest,
    ImportFromTextRequest,
    ImportFromUrlRequest,
    ImportResult,
    SupportedSite,
    UnresolvedEntry,
)
from mpc_forge.services import (
    deck_activity, deck_service,
)
from mpc_forge.services.deck_activity import DeckActivityKind as K


log = logging.getLogger(__name__)


# --- Import / CRUD -------------------------------------------------------

from mpc_forge.routes.decks._views import (
    _deck_to_view,
)
from mpc_forge.routes.decks._common import (
    DbDep, _get_moxfield, _get_scryfall, make_router,
)

router = make_router()


@router.post("/import/moxfield", response_model=ImportResult)
async def import_moxfield(
    payload: ImportFromMoxfieldRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    moxfield: Annotated[MoxfieldClient, Depends(_get_moxfield)],
) -> ImportResult:
    try:
        deck, unresolved = await deck_service.import_from_moxfield(
            db, scryfall, moxfield, payload.url_or_id,
            include_extras=payload.include_extras,
        )
    except MoxfieldError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    # Registro del import en el timeline del propio mazo — el usuario lo verá
    # como primer evento cuando abra su historial.
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "moxfield",
            "source_ref": payload.url_or_id,
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )


@router.post("/import/text", response_model=ImportResult)
async def import_text(
    payload: ImportFromTextRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> ImportResult:
    deck, unresolved = await deck_service.import_from_plaintext(
        db, scryfall, payload.name, payload.text, payload.format,
        include_extras=payload.include_extras,
    )
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "text",
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )


@router.get("/import/supported-sites", response_model=list[SupportedSite])
async def supported_import_sites() -> list[SupportedSite]:
    """Devuelve la lista de sitios que la app sabe importar por URL.

    Usado por el frontend para pintar los ejemplos y validar en cliente que
    el usuario ha pegado una URL de un sitio soportado.
    """
    from mpc_forge.clients.import_sites import list_supported_sites
    return [SupportedSite(**s) for s in list_supported_sites()]


@router.post("/import/url", response_model=ImportResult)
async def import_url(
    payload: ImportFromUrlRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> ImportResult:
    """Import unificado por URL. El sitio se detecta automáticamente.

    Errores:
      - 400 si el hostname no matchea ningún sitio soportado.
      - 502 si el sitio responde mal (mazo privado, timeout, HTML inesperado…).
    """
    from mpc_forge.clients.import_sites.base import ImportSiteError

    try:
        deck, unresolved = await deck_service.import_from_url(
            db, scryfall, payload.url,
            name=payload.name, fmt=payload.format,
            include_extras=payload.include_extras,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except ImportSiteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))

    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db, deck.id, K.DECK_CREATED,
        payload={
            "source": "url",
            "source_ref": payload.url,
            "card_count": resolved_count,
            "unresolved_count": len(unresolved),
            "include_extras": payload.include_extras,
        },
        deck_name=deck.name,
    )
    await db.commit()
    return ImportResult(
        deck=view,
        unresolved=[UnresolvedEntry(**u) for u in unresolved],
        resolved_count=resolved_count,
        total_entries=resolved_count + sum(u["quantity"] for u in unresolved),
    )
