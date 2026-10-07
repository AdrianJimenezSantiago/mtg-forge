from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session

log = logging.getLogger(__name__)

ROUTER_PREFIX = "/api/decks"
ROUTER_TAGS = ["decks"]


def make_router() -> APIRouter:
    return APIRouter(prefix=ROUTER_PREFIX, tags=ROUTER_TAGS)


DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def _get_moxfield(request: Request) -> MoxfieldClient:
    return request.app.state.moxfield


ScryfallDep = Annotated[ScryfallClient, Depends(_get_scryfall)]
MoxfieldDep = Annotated[MoxfieldClient, Depends(_get_moxfield)]
