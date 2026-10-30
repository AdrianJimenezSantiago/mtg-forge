from __future__ import annotations

import hmac
import ipaddress
import logging
from urllib.parse import urlencode

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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


ACCESS_TOKEN_ENV = "MPC_FORGE_ACCESS_TOKEN"  # noqa: S105
EXPOSED_ENV = "MPC_FORGE_EXPOSED"
TOKEN_COOKIE = "mpc_forge_token"  # noqa: S105
TOKEN_QUERY = "token"  # noqa: S105

_LOCKED_PAGE = (
    "<!doctype html><meta charset=utf-8><title>MPC Forge</title>"
    "<body style='font-family:sans-serif;background:#111;color:#ddd;padding:2rem'>"
    "<h1>MPC Forge</h1>"
    "<p>Esta instancia está abierta a la red y pide la clave de acceso. "
    "Abre el enlace con <code>?token=…</code> que aparece en la consola del servidor.</p>"
    "<p>This instance is open to the network and needs its access key. "
    "Open the link with <code>?token=…</code> shown in the server console.</p>"
)


class AccessTokenMiddleware:
    """Pide una clave compartida cuando la app escucha fuera de 127.0.0.1.

    La clave llega una vez en la URL (`?token=`), se guarda en una cookie
    HttpOnly y se quita de la barra de direcciones. Los clientes de la API
    pueden mandarla como `Authorization: Bearer <clave>`.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._token = token.encode()

    def _matches(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(str(value).encode(), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        if self._matches(request.cookies.get(TOKEN_COOKIE)) or self._matches(bearer):
            await self.app(scope, receive, send)
            return
        query_token = request.query_params.get(TOKEN_QUERY)
        if self._matches(query_token):
            params = [(k, v) for k, v in request.query_params.multi_items() if k != TOKEN_QUERY]
            target = request.url.path + (f"?{urlencode(params)}" if params else "")
            response: Response = RedirectResponse(target, status_code=303)
            response.set_cookie(
                TOKEN_COOKIE,
                str(query_token),
                httponly=True,
                samesite="strict",
                max_age=30 * 24 * 3600,
            )
        elif request.url.path.startswith("/api/"):
            response = JSONResponse({"detail": {"code": "access_token_required"}}, status_code=401)
        else:
            response = HTMLResponse(_LOCKED_PAGE, status_code=401)
        await response(scope, receive, send)


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
