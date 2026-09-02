"""Registro y dispatcher de import sites.

Cada sitio se implementa como una subclase de ``ImportSite`` (ver ``base.py``).
Al importarse, se auto-registra en ``_REGISTRY`` mediante la metaclass
``ImportSiteMeta``. El dispatcher ``resolve_site(url)`` detecta cuál usar a
partir del hostname.

Para añadir un sitio nuevo: crea un módulo aquí, hereda de ``ImportSite`` y
declara ``host_names``. Importa el módulo abajo para forzar el registro.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .archidekt import ArchidektSite
from .base import ImportSite, ImportSiteError, InvalidURLError, get_registry
from .cubecobra import CubeCobraSite
from .moxfield import MoxfieldSite
from .mtggoldfish import MTGGoldfishSite
from .scryfall import ScryfallSite
from .tappedout import TappedOutSite


# El registro se rellena por side effect en cada import de arriba. Las clases
# están declaradas con ``__auto_register__ = True`` en base.py, así al definirse
# se añaden ellas mismas al REGISTRY del módulo base.
_REGISTRY = get_registry()


def resolve_site(url: str) -> type[ImportSite] | None:
    """Devuelve la clase ImportSite adecuada para ``url``, o None si no matchea.

    Case-insensitive en el host. Ignora un ``www.`` opcional.
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except (ValueError, AttributeError):
        return None
    if not host:
        return None
    # Match exacto primero
    for site_cls in _REGISTRY:
        for h in site_cls.host_names:
            if host == h.lower():
                return site_cls
    # Match ignorando www.
    stripped = host[4:] if host.startswith("www.") else host
    for site_cls in _REGISTRY:
        for h in site_cls.host_names:
            hh = h[4:] if h.startswith("www.") else h
            if stripped == hh.lower():
                return site_cls
    return None


def list_supported_sites() -> list[dict[str, str]]:
    """Metadata visible en la UI: qué sitios soporta la app y su URL de ejemplo."""
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
    "ImportSite",
    "ImportSiteError",
    "InvalidURLError",
    "resolve_site",
    "list_supported_sites",
    # Sitios individuales (uso puntual)
    "MoxfieldSite",
    "ArchidektSite",
    "CubeCobraSite",
    "MTGGoldfishSite",
    "ScryfallSite",
    "TappedOutSite",
]
