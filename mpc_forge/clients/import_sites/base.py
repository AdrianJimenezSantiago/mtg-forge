"""``ImportSite``: interfaz común para importar mazos desde webs de terceros.

Diseño
------
Cada sitio implementa ``retrieve_card_list(url) -> str`` devolviendo texto
plano en el formato universal de decklists ("Nx Cardname"). El resto de la
pipeline (parseo, resolución con Scryfall, creación del Deck) se reutiliza
tal cual desde ``deck_service.import_from_plaintext``.

Ventajas:
- Un solo path de resolución para todos los orígenes: menos código, menos
  bugs, un solo formato de UnresolvedEntry.
- Añadir un sitio nuevo = ~50-80 líneas.

El patrón viene de ``chilli-axe/mpc-autofill`` (``integrations/game/mtg.py``)
adaptado a nuestro cliente ``httpx`` async y a nuestra estructura.

Auto-registro
-------------
Las subclases se registran solas al declararse gracias al hook
``__init_subclass__``. El módulo ``__init__.py`` importa cada sitio para
disparar el registro.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from mpc_forge import config as cfg
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

# Poblado por __init_subclass__ en cada subclase declarada.
_REGISTRY: list[type["ImportSite"]] = []


def get_registry() -> list[type["ImportSite"]]:
    """Devuelve una copia del registro actual — usado por el dispatcher."""
    return list(_REGISTRY)


class ImportSiteError(RuntimeError):
    """Error genérico al importar de una web de terceros."""


class InvalidURLError(ImportSiteError):
    """La URL no parece corresponder al sitio esperado."""

    def __init__(self, url: str) -> None:
        super().__init__(f"URL no válida para este sitio: {url}")
        self.url = url


def _slug_to_title(slug: str) -> str:
    """Convierte un slug de URL a un título legible.

    Ejemplo: ``my-commander-deck`` → ``My Commander Deck``.
    Trata guiones y guiones bajos como separadores de palabra.
    Devuelve una cadena vacía si el slug no aporta información útil
    (solo contiene dígitos o está vacío).
    """
    words = slug.replace("-", " ").replace("_", " ").split()
    if not words or all(w.isdigit() for w in words):
        return ""
    return " ".join(w.capitalize() for w in words)


class ImportSite:
    """Base abstracta para importadores de mazos.

    Subclases DEBEN definir:
        ``key``          : identificador estable (para logs, telemetry, UI)
        ``name``         : nombre legible ("Moxfield")
        ``host_names``   : hostnames que acepta ("moxfield.com", "www.moxfield.com")
        ``example_url``  : URL de ejemplo mostrada en la UI
        ``retrieve_card_list(url)``: método async que baja el texto plano

    Opcional:
        ``get_headers()`` : cabeceras HTTP adicionales
        ``base_url``      : URL base para requests (usa .request() shortcut)
    """

    # Metadata (obligatoria en subclases)
    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    host_names: ClassVar[tuple[str, ...]] = ()
    example_url: ClassVar[str] = ""
    base_url: ClassVar[str] = ""  # opcional — para el helper .request()

    # Timeout por request. Los sitios lentos como MagicVille pueden necesitar más.
    request_timeout: ClassVar[float] = 30.0

    def __init_subclass__(cls, /, register: bool = True, **kwargs: Any) -> None:
        """Registra la subclase automáticamente al declararse."""
        super().__init_subclass__(**kwargs)
        if not register:
            return
        # Sanity checks tempranos: si una subclase se declara sin la metadata
        # mínima, es un bug del desarrollador — fallamos ruidosamente.
        if not cls.key:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `key`")
        if not cls.name:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `name`")
        if not cls.host_names:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `host_names`")
        _REGISTRY.append(cls)

    # ------------------------------------------------------------------ HTTP
    @classmethod
    def get_headers(cls) -> dict[str, str]:
        """Cabeceras por defecto. Sobreescribir para APIs que exigen tokens."""
        return {
            "User-Agent": cfg.MOXFIELD_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }

    @classmethod
    async def request(
        cls,
        path: str,
        *,
        netloc: str | None = None,
        headers: dict[str, str] | None = None,
        scheme: str = "https",
    ) -> httpx.Response:
        """GET helper. Compone URL a partir de ``base_url`` (o ``netloc`` custom)
        y devuelve la respuesta ya con ``raise_for_status()`` aplicado.

        - Si ``netloc`` se pasa, sobreescribe la parte de host de ``base_url``.
        - ``path`` puede ser absoluto (http…) — si empieza por "http", se usa tal cual.
        """
        merged_headers = {**cls.get_headers(), **(headers or {})}
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            host = netloc or cls.base_url
            if not host:
                raise ImportSiteError(
                    f"{cls.__name__}: no se puede construir URL sin base_url ni netloc"
                )
            if "://" not in host:
                host = f"{scheme}://{host}"
            # normalizar barras: base_url puede o no terminar en /, path puede o no empezar en /
            host = host.rstrip("/")
            path_clean = path if path.startswith("/") else f"/{path}"
            url = f"{host}{path_clean}"

        async with httpx.AsyncClient(
            timeout=cls.request_timeout,
            follow_redirects=True,
            verify=not ssl_insecure(),
            headers=merged_headers,
        ) as client:
            log.debug("[%s] GET %s", cls.key, url)
            resp = await client.get(url)
            resp.raise_for_status()
            return resp

    # ------------------------------------------------------------------ API
    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        """Descarga la decklist como texto plano.

        Formato de salida esperado (unos u otros, mezclados es OK):
            "4 Lightning Bolt"
            "4x Lightning Bolt"
            "1 Sol Ring (C21) 263"
            "//Commander"
            "SB: 1 Blood Moon"

        Los prefijos de sección (Mainboard, Sideboard, //, SB:) los tolera
        ``parse_plain_decklist``. Si el sitio distingue commander/companion,
        preferimos anotar con "//Commanders" antes del bloque.
        """
        raise NotImplementedError

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Devuelve el nombre del mazo tal como lo tiene el sitio origen.

        Por defecto devuelve ``None`` — en ese caso ``import_from_url``
        generará un nombre automático. Los sitios que expongan el nombre en
        su API (Moxfield, Archidekt…) deben sobreescribir este método.

        **Importante**: este método se llama DESPUÉS de ``retrieve_card_list``
        para que los sitios que hayan cacheado el payload durante la primera
        llamada no necesiten hacer un segundo fetch.
        """
        return None
