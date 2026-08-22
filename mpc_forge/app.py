"""Factory de la aplicación FastAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import PATHS
from mpc_forge.db import init_db, session_scope
from mpc_forge.paths import diagnose as paths_diagnose, is_frozen, static_dir
from mpc_forge.routes import custom_art as custom_art_routes
from mpc_forge.routes import debug as debug_routes
from mpc_forge.routes import decks, export, integrations, settings as settings_routes, ui
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

# Configuramos SSL ANTES de crear cualquier cliente HTTPX — así truststore
# inyecta el contexto SSL del sistema (con la CA corporativa si aplica) antes
# de que se instancien conexiones.
_SSL_MODE = configure_ssl()

STATIC_DIR = static_dir()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # File logging PRIMERO: así capturamos también los errores de init_db,
    # settings, etc. La ruta es %APPDATA%/MPC-Forge/logs/mpc-forge.log
    logs_dir = PATHS.data_dir / "logs"
    try:
        log_path = logging_setup.setup_file_logging(logs_dir)
        logging.info("Logging a archivo activo: %s", log_path)
    except Exception as e:  # noqa: BLE001
        logging.warning("No se pudo configurar file logging: %s", e)

    # Si estamos en frozen (release .exe), pinta un dump de las rutas resueltas.
    # Muy útil para diagnosticar problemas de assets/templates que solo aparecen
    # en release y no en dev.
    if is_frozen():
        for line in paths_diagnose().splitlines():
            logging.info(line)

    await init_db()
    # Cargar settings persistidos y aplicarlos a config.py antes de instanciar
    # los clientes (que leen p.ej. USD_TO_EUR o el User-Agent).
    try:
        async with session_scope() as db:
            values = await settings_service.get_all(db)
        settings_service.apply_to_config(values)
    except Exception as e:  # noqa: BLE001
        logging.warning("No se pudieron cargar settings: %s", e)

    app.state.scryfall = ScryfallClient()
    app.state.moxfield = MoxfieldClient()
    app.state.art_cache = ArtCache()
    try:
        async with session_scope() as db:
            stats = await custom_art_service.rescan(db)
        logging.info(
            "Custom art indexado: %d archivos (+%d nuevos, -%d borrados)",
            stats["total"], stats["added"], stats["removed"],
        )
    except Exception as e:  # noqa: BLE001
        logging.warning("Rescan de custom art falló: %s", e)

    # Sembrar sources iniciales (drives de MPCFill) si el usuario no tiene ninguno.
    try:
        async with session_scope() as db:
            seeded = await art_sources_service.seed_initial_if_empty(db)
        if seeded:
            logging.info("Sembrados %d art sources iniciales", seeded)
    except Exception as e:  # noqa: BLE001
        logging.warning("Seed de art sources falló: %s", e)
    logging.info("MPC Forge listo. Datos en: %s", PATHS.data_dir)
    logging.info("SSL: %s", _SSL_MODE)
    try:
        yield
    finally:
        await app.state.scryfall.aclose()
        await app.state.moxfield.aclose()
        await app.state.art_cache.aclose()
        # Shutdown limpio → borramos el log. Si la app crashea antes de llegar
        # aquí, el log queda para diagnóstico post-mortem.
        try:
            logging_setup.teardown_file_logging(delete=True)
        except Exception:  # noqa: BLE001
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="MPC Forge",
        description="Local Magic proxy printing pipeline.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # -- GZip middleware --
    # Comprime responses >1KB. Impacto real:
    #   GET /api/decks/{id} con 100 cartas: 60-80 KB → 8-12 KB (~85% menos)
    #   GET /api/decks/{id}/prints con 900+ prints: 400 KB → 40 KB
    # No comprime imágenes (ya están comprimidas). Cero contra.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

    # Assets con cache aggressive. Un day para /static (JS/CSS que podríamos
    # cambiar entre versiones), un mes para /art y /custom_art (nombres con
    # hash → contenido inmutable).
    app.mount("/static", CachedStaticFiles(directory=str(STATIC_DIR), max_age=86400), name="static")
    app.mount("/art", CachedStaticFiles(directory=str(PATHS.art_dir), max_age=2592000), name="art")
    app.mount("/custom_art", CachedStaticFiles(directory=str(PATHS.custom_art_dir), max_age=2592000), name="custom_art")

    app.include_router(ui.router)
    app.include_router(decks.router)
    app.include_router(export.router)
    app.include_router(custom_art_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(integrations.router)
    app.include_router(debug_routes.router)
    return app


app = create_app()
