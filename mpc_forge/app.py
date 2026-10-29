from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from mpc_forge import __version__
from mpc_forge import config as cfg
from mpc_forge.errors import localize_detail
from mpc_forge.lifespan import lifespan, preload_path_overrides
from mpc_forge.middleware import (
    DEFAULT_ALLOWED_HOSTS,
    USER_CONTENT_SECURITY_POLICY,
    LocalhostGuardMiddleware,
    SecurityHeadersMiddleware,
)
from mpc_forge.paths import static_dir
from mpc_forge.routes import ROUTERS, ui
from mpc_forge.services.art import custom_art
from mpc_forge.services.system.i18n import detect_lang

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

STATIC_DIR = static_dir()

EXTRA_HOSTS_ENV = "MPC_FORGE_ALLOWED_HOSTS"

ONE_DAY = 86_400
THIRTY_DAYS = 30 * ONE_DAY
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

_NON_HTML_PREFIXES = (
    "/api/",
    "/static/",
    "/art/",
    "/custom_art/",
    "/thumbs/",
    "/local-source/",
    "/i18n/",
)


class CachedStaticFiles(StaticFiles):
    def __init__(
        self, *args: Any, max_age: int = ONE_DAY, user_content: bool = False, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._cache_control = f"public, max-age={max_age}"
        self._user_content = user_content

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = IMMUTABLE_CACHE if versioned else self._cache_control
        if self._user_content:
            response.headers["Content-Security-Policy"] = USER_CONTENT_SECURITY_POLICY
        return response


class LinkedCustomArtFiles(CachedStaticFiles):
    """Sirve `/custom_art/_linked/<id>/…` desde la carpeta vinculada `<id>`.

    Solo imágenes, y sin caché larga: el usuario puede editarlas en su carpeta.
    """

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        if not custom_art.is_servable(path):
            return "", None
        resolved = custom_art.resolve_relative(f"{custom_art.LINKED_PREFIX}/{path}")
        if resolved is None:
            return "", None
        try:
            return str(resolved), os.stat(resolved)
        except (FileNotFoundError, NotADirectoryError):
            return "", None


def _wants_html_page(request: Request) -> bool:
    if request.method not in ("GET", "HEAD"):
        return False
    if request.url.path.startswith(_NON_HTML_PREFIXES):
        return False
    return "text/html" in request.headers.get("accept", "")


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404 and _wants_html_page(request):
        return ui.render_not_found(request)
    if isinstance(exc.detail, dict):
        return JSONResponse(
            {"detail": localize_detail(exc.detail, detect_lang(request))},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )
    return await http_exception_handler(request, exc)


def _allowed_hosts() -> frozenset[str]:
    extra = {h.strip().lower() for h in os.environ.get(EXTRA_HOSTS_ENV, "").split(",") if h.strip()}
    if extra:
        log.info("Hosts adicionales permitidos vía %s: %s", EXTRA_HOSTS_ENV, sorted(extra))
    return frozenset(DEFAULT_ALLOWED_HOSTS | extra)


def _mount_static(app: FastAPI) -> None:
    mounts = (
        ("/static", STATIC_DIR, ONE_DAY, "static", False),
        ("/art", cfg.PATHS.art_dir, THIRTY_DAYS, "art", True),
        ("/custom_art", cfg.PATHS.custom_art_dir, THIRTY_DAYS, "custom_art", True),
        ("/thumbs", cfg.PATHS.thumbs_dir, THIRTY_DAYS, "thumbs", True),
    )
    app.mount(
        f"/custom_art/{custom_art.LINKED_PREFIX}",
        LinkedCustomArtFiles(check_dir=False, max_age=0, user_content=True),
        name="custom_art_linked",
    )
    for path, directory, max_age, name, user_content in mounts:
        static = CachedStaticFiles(
            directory=str(directory), max_age=max_age, user_content=user_content
        )
        app.mount(path, static, name=name)


def create_app() -> FastAPI:
    app = FastAPI(
        title="MPC Forge",
        description="Local Magic proxy printing pipeline.",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(LocalhostGuardMiddleware, allowed_hosts=_allowed_hosts())
    _mount_static(app)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    for router in ROUTERS:
        app.include_router(router)
    return app


preload_path_overrides()

app = create_app()
