from __future__ import annotations

import ipaddress
import logging

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
        "[::1]",
        "0.0.0.0",  # noqa: S104
    }
)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# "same-site" no basta: para 127.0.0.1 cualquier otro puerto local (un servidor de
# desarrollo, Jupyter, otra app) es el mismo sitio, pero no el mismo origen.
_SAFE_FETCH_SITES = frozenset({"same-origin", "none"})


NON_LOOPBACK_WARNING = (
    "MPC Forge escucha en %s, fuera de 127.0.0.1: la app no tiene contraseña y "
    "cualquier equipo de la red podrá leer y borrar tus mazos o cambiar los ajustes."
)


def is_loopback_bind(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _hostname(raw: str) -> str:
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


def _cross_origin_reason(request: Request, host_header: str) -> str | None:
    site = request.headers.get("sec-fetch-site", "").lower()
    if site:
        return None if site in _SAFE_FETCH_SITES else f"Sec-Fetch-Site={site}"
    # Navegadores sin Sec-Fetch-* (Safari < 16.4, WebViews antiguos) siguen
    # mandando Origin en los POST; sin ninguna de las dos es un cliente que no es
    # un navegador (curl, scripts) y no hay sitio ajeno que lo dispare.
    origin = request.headers.get("origin")
    if origin is None:
        return None
    try:
        netloc = URL(origin).netloc.lower()
    except Exception:
        netloc = ""
    if not netloc or netloc != host_header.strip().lower():
        return f"Origin={origin}"
    return None


class LocalhostGuardMiddleware(BaseHTTPMiddleware):
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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host_header = request.headers.get("host", "")
        if host_header:
            host = _hostname(host_header)
            if host not in self._allowed:
                log.warning(
                    "Petición rechazada: cabecera Host inesperada %r (posible "
                    "DNS rebinding). Hosts permitidos: %s",
                    host_header,
                    sorted(self._allowed),
                )
                return JSONResponse(
                    {
                        "detail": (
                            "Host no permitido. MPC Forge solo acepta peticiones "
                            "dirigidas a 127.0.0.1 o localhost."
                        )
                    },
                    status_code=400,
                )

        if self._enforce_csrf and request.method in _UNSAFE_METHODS:
            reason = _cross_origin_reason(request, host_header)
            if reason:
                log.warning(
                    "Petición %s %s rechazada: %s (origen cruzado)",
                    request.method,
                    request.url.path,
                    reason,
                )
                return JSONResponse(
                    {"detail": "Petición de origen cruzado rechazada."},
                    status_code=403,
                )

        return await call_next(request)


CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

# Arte y carpetas locales vienen de fuera de la app: aunque se cuele un .html o un
# .js, abierto en el origen de la app no debe poder ejecutar nada.
USER_CONTENT_SECURITY_POLICY = "default-src 'none'; img-src 'self' data:; sandbox"

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *(header for header in SECURITY_HEADERS if header[0] not in present),
                ]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def is_same_origin(request: Request, target: str) -> bool:
    if not target:
        return False
    if target.startswith("//"):
        return False
    if target.startswith("/"):
        return True
    try:
        url = URL(target)
    except Exception:
        return False
    if not url.hostname:
        return False
    return url.netloc.lower() == request.url.netloc.lower()
