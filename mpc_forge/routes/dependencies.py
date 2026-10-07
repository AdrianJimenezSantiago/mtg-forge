from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session
from mpc_forge.services.art.art_cache import ArtCache


def get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def get_moxfield(request: Request) -> MoxfieldClient:
    return request.app.state.moxfield


def get_art_cache(request: Request) -> ArtCache:
    return request.app.state.art_cache


DbDep = Annotated[AsyncSession, Depends(get_session)]
ScryfallDep = Annotated[ScryfallClient, Depends(get_scryfall)]
MoxfieldDep = Annotated[MoxfieldClient, Depends(get_moxfield)]
ArtCacheDep = Annotated[ArtCache, Depends(get_art_cache)]
