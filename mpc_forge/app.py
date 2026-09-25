"""Factory de la aplicación FastAPI."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from mpc_forge import config as cfg
from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import init_db, optimize_db, session_scope
from mpc_forge.middleware import DEFAULT_ALLOWED_HOSTS, LocalhostGuardMiddleware
from mpc_forge.paths import diagnose as paths_diagnose
from mpc_forge.paths import is_frozen, static_dir
from mpc_forge.routes import bulk as bulk_routes
from mpc_forge.routes import collection as collection_routes
from mpc_forge.routes import custom_art as custom_art_routes
from mpc_forge.routes import debug as debug_routes
from mpc_forge.routes import decks, export, integrations, ui
from mpc_forge.routes import library as library_routes
from mpc_forge.routes import planner as planner_routes
from mpc_forge.routes import settings as settings_routes
from mpc_forge.routes import storage as storage_routes
from mpc_forge.routes import thumbs as thumbs_routes
from mpc_forge.services import art_sources as art_sources_service
from mpc_forge.services import custom_art as custom_art_service
from mpc_forge.services import logging_setup
from mpc_forge.services import settings as settings_service
from mpc_forge.services.art_cache import ArtCache
from mpc_forge.ssl_config import configure_ssl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

_SSL_MODE = configure_ssl()

STATIC_DIR = static_dir()

_EXTRA_HOSTS_ENV = "MPC_FORGE_ALLOWED_HOSTS"


def _preload_path_overrides() -> None:
    """Aplica los overrides de paths.* ANTES de que create_app() haga los mounts.

    Sin este preload, los mounts ``/art`` y ``/custom_art`` usarían los paths
    default aunque el usuario tenga custom guardados en BD — porque el lifespan
    de FastAPI corre DESPUÉS del mount de StaticFiles.

    Leemos la BD con sqlite3 síncrono (no aiosqlite) — está bien porque solo
    hacemos una SELECT rápida antes de que el event loop arranque. Si la BD no
    existe (primera ejecución) o la tabla kv_store aún no está creada,
    simplemente no hay overrides que aplicar.
    """
    if not cfg.PATHS.db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(cfg.PATHS.db_path))
        try:
            rows = conn.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE 'settings.paths.%'"
            ).fetchall()
        finally:
            conn.close()
        overrides = {}
        for key, value in rows:
            short = key[len("settings.paths."):]
            if value and value.strip():
                overrides[short] = value.strip()
        if overrides:
            cfg.PATHS = cfg.Paths.default().with_overrides(**overrides)
            log.info("Aplicados %d overrides de paths desde BD", len(overrides))
    except sqlite3.OperationalError:
        pass
    except Exception as e:
        log.warning("Preload de path overrides falló: %s", e)


_preload_path_overrides()


class BackgroundTasks:
    """Registro de tareas de background con referencia fuerte.

    ``asyncio`` solo guarda una referencia DÉBIL a las tareas creadas con
    ``create_task``. Si nadie más las referencia, el recolector de basura puede
    llevárselas a mitad de ejecución: la tarea desaparece sin excepción, sin
    log y sin dejar rastro. Es un bug intermitente clásico y casi imposible de
    diagnosticar desde un reporte de usuario.

    Esta clase mantiene el set vivo hasta que cada tarea termina, registra las
    excepciones que se hayan tragado, y permite cancelarlas todas ordenadamente
    en el shutdown para que no sigan escribiendo en una BD que ya se cierra.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "La tarea de background %r terminó con excepción",
                task.get_name(), exc_info=exc,
            )

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Cancela las tareas pendientes y espera a que suelten sus recursos."""
        pending = [t for t in self._tasks if not t.done()]
        if not pending:
            return
        log.info("Cancelando %d tareas de background…", len(pending))
        for task in pending:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout
            )
        except TimeoutError:
            log.warning(
                "%d tareas no respondieron a la cancelación en %.0fs; "
                "se continúa con el cierre.", len(pending), timeout,
            )


def _is_benign_connection_reset(context: dict) -> bool:
    """True para el ``ConnectionResetError`` que asyncio registra en Windows
    cuando el navegador cierra una conexión de golpe.

    El bucle Proactor, al cerrar el transporte, llama a ``socket.shutdown()``
    sobre un socket que el otro extremo ya reseteó (WinError 10054) y lo
    reporta como ERROR con traza completa. La petición ya se respondió; no hay
    nada que hacer salvo no asustar al usuario en el log.
    """
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False
    where = f"{context.get('handle', '')} {context.get('message', '')}"
    return "_call_connection_lost" in where


def _silence_windows_connection_resets(loop: asyncio.AbstractEventLoop) -> None:
    """Instala un exception handler que descarta solo ese caso benigno."""
    if sys.platform != "win32":
        return
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        if _is_benign_connection_reset(context):
            log.debug("Conexión cerrada por el cliente: %s", context.get("exception"))
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _silence_windows_connection_resets(asyncio.get_running_loop())
    logs_dir = cfg.PATHS.data_dir / "logs"
    try:
        log_path = logging_setup.setup_file_logging(logs_dir)
        log.info("Logging a archivo activo: %s", log_path)
    except Exception as e:
        log.warning("No se pudo configurar file logging: %s", e)

    if is_frozen():
        for line in paths_diagnose().splitlines():
            log.info(line)

    await init_db()
    try:
        async with session_scope() as db:
            values = await settings_service.get_all(db)
        settings_service.apply_to_config(values)
    except Exception as e:
        log.warning("No se pudieron cargar settings: %s", e)

    app.state.scryfall = ScryfallClient()
    app.state.moxfield = MoxfieldClient()
    app.state.art_cache = ArtCache()
    try:
        async with session_scope() as db:
            stats = await custom_art_service.rescan(db)
        log.info(
            "Custom art indexado: %d archivos (+%d nuevos, -%d borrados)",
            stats["total"], stats["added"], stats["removed"],
        )
    except Exception as e:
        log.warning("Rescan de custom art falló: %s", e)

    try:
        async with session_scope() as db:
            seeded = await art_sources_service.seed_initial_if_empty(db)
        if seeded:
            log.info("Sembrados %d art sources iniciales", seeded)
    except Exception as e:
        log.warning("Seed de art sources falló: %s", e)

    async def _run_backfill():
        try:
            from mpc_forge.services import gdrive_indexer
            async with session_scope() as db:
                await gdrive_indexer.backfill_normalized_names(db)
        except Exception as e:
            log.warning("Backfill de normalización falló: %s", e)
    app.state.background = BackgroundTasks()
    app.state.background.spawn(_run_backfill(), name="normalization-backfill")

    async def _run_dfc_sync():
        try:
            from mpc_forge.services import dfc_pairs
            async with session_scope() as db:
                await dfc_pairs.sync_if_stale(db, app.state.scryfall)
        except Exception as e:
            log.warning("Sync de DFC pairs falló: %s", e)
    app.state.background.spawn(_run_dfc_sync(), name="dfc-pairs-sync")

    log.info("MPC Forge listo. Datos en: %s", cfg.PATHS.data_dir)
    log.info("SSL: %s", _SSL_MODE)
    try:
        yield
    finally:
        await app.state.background.shutdown()
        await app.state.scryfall.aclose()
        await app.state.moxfield.aclose()
        await app.state.art_cache.aclose()
        await optimize_db()
        try:
            logging_setup.teardown_file_logging(delete=True)
        except Exception:
            pass


class CachedStaticFiles(StaticFiles):
    """StaticFiles con Cache-Control para que el navegador no re-descargue
    los assets en cada refresh.

    - /static/ (nuestros JS/CSS/imágenes) → 1 día (max-age=86400)
    - /art/ y /custom_art/ (imágenes de cartas) → 30 días (max-age=2592000)
      porque tienen un hash/UUID en el nombre y son efectivamente inmutables.
    """
    def __init__(self, *args, max_age: int = 86400, **kwargs):
        super().__init__(*args, **kwargs)
        self._max_age = max_age

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = f"public, max-age={self._max_age}"
        return response


_NON_HTML_PREFIXES = (
    "/api/", "/static/", "/art/", "/custom_art/", "/thumbs/",
    "/local-source/", "/i18n/",
)


def _wants_html_page(request: Request) -> bool:
    """¿Es una navegación del usuario que merece la página 404 con estilo?"""
    if request.method not in ("GET", "HEAD"):
        return False
    if request.url.path.startswith(_NON_HTML_PREFIXES):
        return False
    return "text/html" in request.headers.get("accept", "")


async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """404 del navegador → plantilla propia. Todo lo demás, como antes."""
    if exc.status_code == 404 and _wants_html_page(request):
        return ui.render_not_found(request)
    return await http_exception_handler(request, exc)


def create_app() -> FastAPI:
    app = FastAPI(
        title="MPC Forge",
        description="Local Magic proxy printing pipeline.",
        version="0.1.0",
        lifespan=lifespan,
    )

    extra_hosts = {
        h.strip().lower()
        for h in os.environ.get(_EXTRA_HOSTS_ENV, "").split(",")
        if h.strip()
    }
    if extra_hosts:
        log.info(
            "Hosts adicionales permitidos vía %s: %s",
            _EXTRA_HOSTS_ENV, sorted(extra_hosts),
        )

    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    app.add_middleware(
        LocalhostGuardMiddleware,
        allowed_hosts=frozenset(DEFAULT_ALLOWED_HOSTS | extra_hosts),
    )

    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR), max_age=86400), name="static")
    app.mount("/art", CachedStaticFiles(directory=str(cfg.PATHS.art_dir), max_age=2592000), name="art")
    app.mount("/custom_art", CachedStaticFiles(directory=str(cfg.PATHS.custom_art_dir), max_age=2592000), name="custom_art")
    app.mount("/thumbs", CachedStaticFiles(directory=str(cfg.PATHS.thumbs_dir), max_age=2592000), name="thumbs")

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)

    app.include_router(ui.router)
    app.include_router(decks.router)
    app.include_router(export.router)
    app.include_router(custom_art_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(integrations.router)
    app.include_router(integrations.local_source_router)
    app.include_router(collection_routes.router)
    app.include_router(planner_routes.router)
    app.include_router(library_routes.router)
    app.include_router(bulk_routes.router)
    app.include_router(thumbs_routes.router)
    app.include_router(storage_routes.router)
    app.include_router(debug_routes.router)
    return app


app = create_app()
