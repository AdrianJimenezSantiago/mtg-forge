"""Middleware de seguridad para un servidor que escucha en localhost.

Modelo de amenaza
-----------------
La app abre un puerto HTTP en 127.0.0.1 sin autenticación: cualquier cosa que
consiga hablar con él tiene acceso total a los mazos, las rutas del sistema de
ficheros y la API key de Drive. "Solo escucha en local" protege de la red, pero
no del navegador que el propio usuario tiene abierto:

1. **DNS rebinding.** Una web del atacante se sirve desde un dominio cuyo DNS
   resuelve primero a su servidor y, segundos después, a 127.0.0.1. Para el
   navegador sigue siendo *el mismo origen*, así que la política de mismo
   origen no impide leer las respuestas. Sin CORS configurado no hay nada que
   lo pare — la defensa correcta es validar la cabecera ``Host``, que en ese
   ataque es el dominio del atacante y no ``127.0.0.1``.

2. **CSRF.** Sin CORS, un ``fetch`` cross-origin con ``Content-Type:
   application/json`` dispara preflight y se bloquea. Pero un formulario HTML
   puede hacer POST cross-origin sin preflight. ``Sec-Fetch-Site`` (soportado
   por todos los navegadores actuales) distingue eso de una petición legítima
   de la propia UI.

Ambas comprobaciones son baratas y no afectan al uso normal.
"""
from __future__ import annotations

import logging

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger(__name__)

# Hosts que se consideran "esta máquina". Se comparan sin puerto.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "[::1]",
    # No es un bind: es la lista de nombres que aceptamos en la cabecera
    # Host cuando el usuario arranca con `--host 0.0.0.0` a propósito.
    "0.0.0.0",  # noqa: S104 — nombre de host aceptado, no un bind
})

# Métodos que modifican estado y por tanto necesitan la comprobación anti-CSRF.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Valores de Sec-Fetch-Site que significan "esto lo ha originado nuestra propia
# página". `none` es navegación directa (el usuario escribió la URL o abrió un
# marcador), que también aceptamos.
_SAFE_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})


def _hostname(raw: str) -> str:
    """Extrae el hostname de una cabecera Host, quitando el puerto.

    Cuidado con IPv6: ``[::1]:8765`` tiene dos puntos dentro de los corchetes,
    así que no vale con partir por el primer ``:``.
    """
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


class LocalhostGuardMiddleware(BaseHTTPMiddleware):
    """Rechaza peticiones con un ``Host`` desconocido o de origen cruzado.

    ``extra_hosts`` permite al usuario abrir la app desde otro equipo de su LAN
    (``--host 0.0.0.0``) sin desactivar la protección: quien hace eso lo hace a
    propósito y puede declarar el nombre con el que se accede.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: frozenset[str] | None = None,
        enforce_csrf: bool = True,
    ) -> None:
        super().__init__(app)
        self._allowed = set(allowed_hosts or DEFAULT_ALLOWED_HOSTS)
        self._enforce_csrf = enforce_csrf

    async def dispatch(self, request: Request, call_next) -> Response:
        host_header = request.headers.get("host", "")
        if host_header:
            host = _hostname(host_header)
            if host not in self._allowed:
                log.warning(
                    "Petición rechazada: cabecera Host inesperada %r (posible "
                    "DNS rebinding). Hosts permitidos: %s",
                    host_header, sorted(self._allowed),
                )
                return JSONResponse(
                    {"detail": (
                        "Host no permitido. MPC Forge solo acepta peticiones "
                        "dirigidas a 127.0.0.1 o localhost."
                    )},
                    status_code=400,
                )

        if self._enforce_csrf and request.method in _UNSAFE_METHODS:
            site = request.headers.get("sec-fetch-site", "").lower()
            if site and site not in _SAFE_FETCH_SITES:
                log.warning(
                    "Petición %s %s rechazada: Sec-Fetch-Site=%s (origen cruzado)",
                    request.method, request.url.path, site,
                )
                return JSONResponse(
                    {"detail": "Petición de origen cruzado rechazada."},
                    status_code=403,
                )

        return await call_next(request)


def is_same_origin(request: Request, target: str) -> bool:
    """True si ``target`` apunta al propio servidor (o es una ruta relativa).

    Se usa para validar redirecciones construidas a partir de cabeceras que
    controla el cliente, como ``Referer``.
    """
    if not target:
        return False
    if target.startswith("//"):
        # "//evil.com/x" es una URL absoluta con esquema heredado, no una ruta.
        return False
    if target.startswith("/"):
        return True
    try:
        url = URL(target)
    except Exception:
        return False
    if not url.hostname:
        return False
    return _hostname(url.netloc) == _hostname(request.url.netloc)
