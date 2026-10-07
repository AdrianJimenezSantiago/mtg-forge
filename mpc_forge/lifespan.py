from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mpc_forge import config as cfg
from mpc_forge.clients.moxfield import MoxfieldClient
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import init_db, optimize_db, session_scope
from mpc_forge.paths import diagnose as paths_diagnose
from mpc_forge.paths import is_frozen
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.art.art_cache import ArtCache
from mpc_forge.services.cards import dfc_pairs
from mpc_forge.services.indexing import art_sources as art_sources_service
from mpc_forge.services.indexing import gdrive_indexer
from mpc_forge.services.system import logging_setup
from mpc_forge.services.system import settings as settings_service
from mpc_forge.ssl_config import configure_ssl
from mpc_forge.utils.background import BackgroundTasks
from mpc_forge.utils.event_loop import silence_windows_connection_resets

log = logging.getLogger(__name__)

SSL_MODE = configure_ssl()

_PATH_SETTING_PREFIX = "settings.paths."


def preload_path_overrides() -> None:
    if not cfg.PATHS.db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(cfg.PATHS.db_path))
        try:
            rows = conn.execute(
                "SELECT key, value FROM kv_store WHERE key LIKE ?",
                (f"{_PATH_SETTING_PREFIX}%",),
            ).fetchall()
        finally:
            conn.close()
        overrides = {
            key[len(_PATH_SETTING_PREFIX) :]: value.strip()
            for key, value in rows
            if value and value.strip()
        }
        if overrides:
            cfg.PATHS = cfg.Paths.default().with_overrides(**overrides)
            log.info("Aplicados %d overrides de paths desde BD", len(overrides))
    except sqlite3.OperationalError:
        pass
    except Exception as e:
        log.warning("Preload de path overrides falló: %s", e)


def _start_file_logging() -> None:
    try:
        log_path = logging_setup.setup_file_logging(cfg.PATHS.data_dir / "logs")
        log.info("Logging a archivo activo: %s", log_path)
    except Exception as e:
        log.warning("No se pudo configurar file logging: %s", e)


async def _load_settings() -> None:
    try:
        async with session_scope() as db:
            values = await settings_service.get_all(db)
        settings_service.apply_to_config(values)
    except Exception as e:
        log.warning("No se pudieron cargar settings: %s", e)


async def _rescan_custom_art() -> None:
    try:
        async with session_scope() as db:
            stats = await custom_art_service.rescan(db)
        log.info(
            "Custom art indexado: %d archivos (+%d nuevos, -%d borrados)",
            stats["total"],
            stats["added"],
            stats["removed"],
        )
    except Exception as e:
        log.warning("Rescan de custom art falló: %s", e)


async def _seed_art_sources() -> None:
    try:
        async with session_scope() as db:
            seeded = await art_sources_service.seed_initial_if_empty(db)
        if seeded:
            log.info("Sembrados %d art sources iniciales", seeded)
    except Exception as e:
        log.warning("Seed de art sources falló: %s", e)


async def _backfill_normalized_names() -> None:
    try:
        async with session_scope() as db:
            await gdrive_indexer.backfill_normalized_names(db)
    except Exception as e:
        log.warning("Backfill de normalización falló: %s", e)


async def _sync_dfc_pairs(scryfall: ScryfallClient) -> None:
    try:
        async with session_scope() as db:
            await dfc_pairs.sync_if_stale(db, scryfall)
    except Exception as e:
        log.warning("Sync de DFC pairs falló: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    silence_windows_connection_resets(asyncio.get_running_loop())
    _start_file_logging()
    if is_frozen():
        for line in paths_diagnose().splitlines():
            log.info(line)

    await init_db()
    await _load_settings()

    app.state.scryfall = ScryfallClient()
    app.state.moxfield = MoxfieldClient()
    app.state.art_cache = ArtCache()
    await _rescan_custom_art()
    await _seed_art_sources()

    app.state.background = BackgroundTasks()
    app.state.background.spawn(_backfill_normalized_names(), name="normalization-backfill")
    app.state.background.spawn(_sync_dfc_pairs(app.state.scryfall), name="dfc-pairs-sync")

    log.info("MPC Forge listo. Datos en: %s", cfg.PATHS.data_dir)
    log.info("SSL: %s", SSL_MODE)
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
