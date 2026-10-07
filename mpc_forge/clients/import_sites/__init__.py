from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .archidekt import ArchidektSite
from .base import ImportSite, ImportSiteError, InvalidURLError, get_registry
from .cubecobra import CubeCobraSite
from .deckstats import DeckstatsSite
from .moxfield import MoxfieldSite
from .mtggoldfish import MTGGoldfishSite
from .scryfall import ScryfallSite
from .tappedout import TappedOutSite

_REGISTRY = get_registry()


def resolve_site(url: str) -> type[ImportSite] | None:
    try:
        host = (urlparse(url).netloc or "").lower()
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    for site_cls in _REGISTRY:
        for h in site_cls.host_names:
            if host == h.lower():
                return site_cls
    stripped = host[4:] if host.startswith("www.") else host
    for site_cls in _REGISTRY:
        for h in site_cls.host_names:
            hh = h[4:] if h.startswith("www.") else h
            if stripped == hh.lower():
                return site_cls
    return None


def list_supported_sites() -> list[dict[str, Any]]:
    return [
        {
            "key": cls.key,
            "name": cls.name,
            "example_url": cls.example_url,
            "host_names": list(cls.host_names),
        }
        for cls in _REGISTRY
    ]


__all__ = [
    "ArchidektSite",
    "CubeCobraSite",
    "DeckstatsSite",
    "ImportSite",
    "ImportSiteError",
    "InvalidURLError",
    "MTGGoldfishSite",
    "MoxfieldSite",
    "ScryfallSite",
    "TappedOutSite",
    "list_supported_sites",
    "resolve_site",
]
