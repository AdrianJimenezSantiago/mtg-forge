from __future__ import annotations

import logging

from fastapi import (
    HTTPException,
    status,
)

from mpc_forge.clients.import_sites import list_supported_sites
from mpc_forge.clients.import_sites.base import ImportSiteError
from mpc_forge.clients.moxfield import MoxfieldError
from mpc_forge.routes.decks._common import make_router
from mpc_forge.routes.decks._views import _deck_to_view
from mpc_forge.routes.dependencies import DbDep, MoxfieldDep, ScryfallDep
from mpc_forge.schemas import (
    ImportFromMoxfieldRequest,
    ImportFromTextRequest,
    ImportFromUrlRequest,
    ImportResult,
    SupportedSite,
    UnresolvedEntry,
)
from mpc_forge.services.decks import deck_activity, importer
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


router = make_router()


@router.post("/import/moxfield", response_model=ImportResult)
async def import_moxfield(
    payload: ImportFromMoxfieldRequest,
    db: DbDep,
    scryfall: ScryfallDep,
    moxfield: MoxfieldDep,
) -> ImportResult:
    try:
        deck, unresolved = await importer.import_from_moxfield(
            db,
            scryfall,
            moxfield,
            payload.url_or_id,
            include_extras=payload.include_extras,
        )
    except MoxfieldError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db,
        deck.id,
        K.DECK_CREATED,
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
    scryfall: ScryfallDep,
) -> ImportResult:
    deck, unresolved = await importer.import_from_plaintext(
        db,
        scryfall,
        payload.name,
        payload.text,
        payload.format,
        include_extras=payload.include_extras,
    )
    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db,
        deck.id,
        K.DECK_CREATED,
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
    return [SupportedSite(**s) for s in list_supported_sites()]


@router.post("/import/url", response_model=ImportResult)
async def import_url(
    payload: ImportFromUrlRequest,
    db: DbDep,
    scryfall: ScryfallDep,
) -> ImportResult:
    try:
        deck, unresolved = await importer.import_from_url(
            db,
            scryfall,
            payload.url,
            name=payload.name,
            fmt=payload.format,
            include_extras=payload.include_extras,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except ImportSiteError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    view = await _deck_to_view(db, deck)
    resolved_count = sum(c.quantity for c in view.cards)
    await deck_activity.log_event(
        db,
        deck.id,
        K.DECK_CREATED,
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
