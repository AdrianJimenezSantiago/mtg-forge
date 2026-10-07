from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mpc_forge import config as cfg
from mpc_forge import migrations
from mpc_forge.models import Base

log = logging.getLogger(__name__)

SCHEMA_VERSION = str(migrations.LATEST_VERSION)

DATABASE_URL = f"sqlite+aiosqlite:///{cfg.PATHS.db_path}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30.0},
)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-20000")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


EXTRA_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_id ON deck_cards(deck_id)",
    "CREATE INDEX IF NOT EXISTS ix_deck_cards_scryfall_id ON deck_cards(scryfall_id)",
    "CREATE INDEX IF NOT EXISTS ix_deck_cards_role ON deck_cards(role)",
    "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_role ON deck_cards(deck_id, role)",
    "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_include ON deck_cards(deck_id, include)",
    "CREATE INDEX IF NOT EXISTS ix_deck_activity_deck_created ON deck_activity(deck_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_decks_updated_at ON decks(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_printings_locate "
    "ON printings(oracle_id, set_code, collector_number, lang)",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_full_art "
    "ON indexed_art(name_normalized) WHERE is_full_art=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_borderless "
    "ON indexed_art(name_normalized) WHERE is_borderless=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_extended "
    "ON indexed_art(name_normalized) WHERE is_extended=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_showcase "
    "ON indexed_art(name_normalized) WHERE is_showcase=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_retro "
    "ON indexed_art(name_normalized) WHERE is_retro=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_textless "
    "ON indexed_art(name_normalized) WHERE is_textless=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_promo "
    "ON indexed_art(name_normalized) WHERE is_promo=1",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_alt_art "
    "ON indexed_art(name_normalized) WHERE is_alt_art=1",
    "CREATE INDEX IF NOT EXISTS ix_dfc_pairs_front_lower ON dfc_pairs(lower(front_name))",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_expansion "
    "ON indexed_art(expansion_code) WHERE expansion_code IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_phash "
    "ON indexed_art(image_hash) WHERE image_hash IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_indexed_art_card_type ON indexed_art(card_type)",
    "CREATE INDEX IF NOT EXISTS ix_collection_set ON collection_entries(set_code)",
    "CREATE INDEX IF NOT EXISTS ix_collection_oracle ON collection_entries(oracle_id)",
    "CREATE INDEX IF NOT EXISTS ix_print_run_items_run ON print_run_items(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_print_runs_created ON print_runs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_printings_name_lower ON printings(lower(name))",
]


LAST_MIGRATION_REPORT: dict[str, object] = {}


async def init_db() -> None:
    global LAST_MIGRATION_REPORT

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _make_backup():
            from mpc_forge.services.system import backup as backup_service

            return backup_service.create_backup(tag="pre-migration")

        report = await migrations.run(conn, on_backup=_make_backup)
        LAST_MIGRATION_REPORT = report

        if report.get("applied"):
            log.info(
                "Migraciones aplicadas: %s (v%s → v%s)",
                report["applied"],
                report["from_version"],
                report["to_version"],
            )

        for stmt in EXTRA_INDEXES:
            await conn.execute(text(stmt))

        fts5_ok = await _try_setup_fts5(conn)
        await conn.execute(
            text("INSERT OR REPLACE INTO kv_store (key, value) VALUES ('fts5_available', :v)"),
            {"v": "1" if fts5_ok else "0"},
        )


async def optimize_db() -> None:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA optimize"))
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        log.info("Mantenimiento de BD completado (optimize + wal checkpoint)")
    except Exception:
        log.exception("El mantenimiento de cierre de la BD falló (no es crítico)")


async def analyze_table(table: str) -> None:
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"ANALYZE {table}"))
        log.info("ANALYZE %s completado", table)
    except Exception:
        log.exception("ANALYZE %s falló (no es crítico)", table)


async def _try_setup_fts5(conn) -> bool:
    try:
        await conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS indexed_art_fts USING fts5("
                "  name_normalized,"
                "  filename,"
                "  tags,"
                "  content='indexed_art',"
                "  content_rowid='id',"
                "  tokenize='unicode61 remove_diacritics 2'"
                ")"
            )
        )
    except Exception as e:
        log.warning(
            "FTS5 no disponible en esta build de SQLite (%s). La búsqueda usará "
            "el modo LIKE. Actualiza el runtime si tienes muchos drives — "
            "FTS5 acelera 10-100x las queries.",
            e,
        )
        return False

    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_insert "
            "AFTER INSERT ON indexed_art BEGIN "
            "  INSERT INTO indexed_art_fts(rowid, name_normalized, filename, tags) "
            "  VALUES (new.id, new.name_normalized, new.filename, new.tags); "
            "END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_delete "
            "AFTER DELETE ON indexed_art BEGIN "
            "  INSERT INTO indexed_art_fts(indexed_art_fts, rowid, name_normalized, filename, tags) "
            "  VALUES ('delete', old.id, old.name_normalized, old.filename, old.tags); "
            "END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_update "
            "AFTER UPDATE ON indexed_art BEGIN "
            "  INSERT INTO indexed_art_fts(indexed_art_fts, rowid, name_normalized, filename, tags) "
            "  VALUES ('delete', old.id, old.name_normalized, old.filename, old.tags); "
            "  INSERT INTO indexed_art_fts(rowid, name_normalized, filename, tags) "
            "  VALUES (new.id, new.name_normalized, new.filename, new.tags); "
            "END"
        )
    )

    real_count = int((await conn.execute(text("SELECT COUNT(*) FROM indexed_art"))).scalar() or 0)
    fts_count = int(
        (await conn.execute(text("SELECT COUNT(*) FROM indexed_art_fts"))).scalar() or 0
    )
    if real_count > 0 and fts_count == 0:
        log.info(
            "FTS5 vacío pero %d filas en indexed_art — rebuild de la tabla virtual…", real_count
        )
        await conn.execute(text("INSERT INTO indexed_art_fts(indexed_art_fts) VALUES ('rebuild')"))
        log.info("Rebuild de FTS5 completo")

    return True


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
