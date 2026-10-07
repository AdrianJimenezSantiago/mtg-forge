from __future__ import annotations

from enum import Enum

from fastapi import APIRouter

ROUTER_PREFIX = "/api/decks"
ROUTER_TAGS: list[str | Enum] = ["decks"]


def make_router() -> APIRouter:
    return APIRouter(prefix=ROUTER_PREFIX, tags=ROUTER_TAGS)
