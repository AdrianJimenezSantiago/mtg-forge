from __future__ import annotations

import logging

from starlette.datastructures import URL
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

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

_SAFE_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})


def _hostname(raw: str) -> str:
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
    if ":" in host:
        return host.rsplit(":", 1)[0]
    return host


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
            site = request.headers.get("sec-fetch-site", "").lower()
            if site and site not in _SAFE_FETCH_SITES:
                log.warning(
                    "Petición %s %s rechazada: Sec-Fetch-Site=%s (origen cruzado)",
                    request.method,
                    request.url.path,
                    site,
                )
                return JSONResponse(
                    {"detail": "Petición de origen cruzado rechazada."},
                    status_code=403,
                )

        return await call_next(request)


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
    return _hostname(url.netloc) == _hostname(request.url.netloc)
