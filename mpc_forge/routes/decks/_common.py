"""Piezas compartidas por los sub-routers de mazos.

Aquí viven las dependencias de FastAPI y los tipos que usan varios módulos del
paquete. Tenerlas en un sitio evita el ciclo de importación que aparecería si
un sub-router importara de otro.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session

log = logging.getLogger(__name__)

# Todos los sub-routers comparten prefijo y etiqueta: las rutas resultantes son
# exactamente las mismas que cuando todo vivía en un único fichero.
ROUTER_PREFIX = "/api/decks"
ROUTER_TAGS = ["decks"]


def make_router() -> APIRouter:
    """Un router con el prefijo y las etiquetas comunes ya aplicados."""
    return APIRouter(prefix=ROUTER_PREFIX, tags=ROUTER_TAGS)


DbDep = Annotated[AsyncSession, Depends(get_session)]


def _get_scryfall(request: Request) -> ScryfallClient:
    return request.app.state.scryfall


def _get_moxfield(request: Request) -> MoxfieldClient:
    return request.app.state.moxfield


ScryfallDep = Annotated[ScryfallClient, Depends(_get_scryfall)]
MoxfieldDep = Annotated[MoxfieldClient, Depends(_get_moxfield)]
