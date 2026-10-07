from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text

log = logging.getLogger(__name__)

BASELINE_VERSION = 8


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: list[str] = field(default_factory=list)
    callback: Callable[[object], Awaitable[None]] | None = None


async def column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return column in {row[1] for row in result}


async def table_exists(conn, table: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )
    return result.first() is not None


async def add_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:
    if not await table_exists(conn, table):
        return False
    if await column_exists(conn, table, column):
        return False
    log.info("Migración: ADD COLUMN %s.%s", table, column)
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return True


LEGACY_COLUMNS: list[tuple[str, str, str]] = [
    ("decks", "custom_cardback_art_id", "INTEGER REFERENCES custom_arts(id) ON DELETE SET NULL"),
    ("indexed_art", "tags", "VARCHAR(512) DEFAULT ''"),
    ("indexed_art", "is_full_art", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_borderless", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_extended", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_showcase", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_retro", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_textless", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_promo", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "is_alt_art", "BOOLEAN DEFAULT 0"),
    ("indexed_art", "expansion_code", "VARCHAR(8) DEFAULT NULL"),
    ("indexed_art", "collector_number", "VARCHAR(16) DEFAULT NULL"),
    ("indexed_art", "canonical_source", "VARCHAR(16) DEFAULT ''"),
    ("indexed_art", "image_hash", "VARCHAR(16) DEFAULT NULL"),
    ("indexed_art", "download_url", "VARCHAR(1024) DEFAULT NULL"),
    ("indexed_art", "thumb_url", "VARCHAR(1024) DEFAULT NULL"),
    ("indexed_art", "card_type", "VARCHAR(16) DEFAULT 'CARD'"),
]


MIGRATIONS: list[Migration] = [
    Migration(
        version=9,
        description="Snapshots de mazo (versionado) + thumbnails de arte",
        statements=[
            """
            CREATE TABLE IF NOT EXISTS deck_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deck_id INTEGER REFERENCES decks(id) ON DELETE CASCADE,
                label VARCHAR(256) NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                card_count INTEGER NOT NULL DEFAULT 0,
                auto INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_deck_snapshots_deck "
            "ON deck_snapshots(deck_id, created_at DESC)",
            "ALTER TABLE local_arts ADD COLUMN thumb_path TEXT DEFAULT NULL",
        ],
    ),
    Migration(
        version=10,
        description="Temas de arte guardados (ArtTheme + ArtThemeEntry)",
        statements=[
            """
            CREATE TABLE IF NOT EXISTS art_themes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(128) NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                entry_count INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS art_theme_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                theme_id INTEGER NOT NULL
                    REFERENCES art_themes(id) ON DELETE CASCADE,
                oracle_id VARCHAR(64) NOT NULL,
                card_name VARCHAR(256) NOT NULL DEFAULT '',
                scryfall_id VARCHAR(64) DEFAULT NULL,
                custom_art_front_id INTEGER DEFAULT NULL,
                custom_art_back_id INTEGER DEFAULT NULL
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_art_theme_entry "
            "ON art_theme_entries(theme_id, oracle_id)",
        ],
    ),
    Migration(
        version=11,
        description="Metadatos de sincronización de bulk data de Scryfall",
        statements=[
            """
            CREATE TABLE IF NOT EXISTS bulk_sync_state (
                kind VARCHAR(32) PRIMARY KEY,
                updated_at VARCHAR(64) NOT NULL DEFAULT '',
                synced_at DATETIME,
                rows_imported INTEGER NOT NULL DEFAULT 0,
                bytes_downloaded INTEGER NOT NULL DEFAULT 0
            )
            """,
        ],
    ),
    Migration(
        version=12,
        description="Precios de mercado y legalidades por formato en printings",
        statements=[
            "ALTER TABLE printings ADD COLUMN price_usd FLOAT",
            "ALTER TABLE printings ADD COLUMN price_usd_foil FLOAT",
            "ALTER TABLE printings ADD COLUMN price_eur FLOAT",
            "ALTER TABLE printings ADD COLUMN legalities TEXT NOT NULL DEFAULT ''",
        ],
    ),
]

LATEST_VERSION = max([m.version for m in MIGRATIONS], default=BASELINE_VERSION)


async def read_version(conn) -> int | None:
    if not await table_exists(conn, "kv_store"):
        return None
    row = (
        await conn.execute(text("SELECT value FROM kv_store WHERE key='schema_version'"))
    ).first()
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return BASELINE_VERSION


async def stamp_version(conn, version: int) -> None:
    await conn.execute(
        text("INSERT OR REPLACE INTO kv_store (key, value) VALUES ('schema_version', :v)"),
        {"v": str(version)},
    )


async def adopt_legacy(conn) -> None:
    added = 0
    for table, column, ddl in LEGACY_COLUMNS:
        if await add_column_if_missing(conn, table, column, ddl):
            added += 1
    if added:
        log.info("Adopción legacy: %d columnas añadidas sin pérdida de datos", added)


async def run(conn, *, on_backup=None) -> dict[str, object]:
    current = await read_version(conn)
    report: dict[str, object] = {
        "from_version": current,
        "to_version": LATEST_VERSION,
        "applied": [],
        "backup_path": None,
        "fresh": current is None,
    }

    if current is None:
        await stamp_version(conn, LATEST_VERSION)
        log.info("BD nueva inicializada en la versión %d", LATEST_VERSION)
        return report

    if current > LATEST_VERSION:
        log.warning(
            "La BD está en la versión %d, más nueva que la que soporta esta "
            "build (%d). Se continúa sin migrar — considera actualizar la app.",
            current,
            LATEST_VERSION,
        )
        return report

    pending = [m for m in MIGRATIONS if m.version > current]

    if current < BASELINE_VERSION:
        log.info(
            "BD del sistema antiguo (v%s). Adoptando a v%d sin borrar datos.",
            current,
            BASELINE_VERSION,
        )
        if on_backup is not None and report["backup_path"] is None:
            report["backup_path"] = _safe_backup(on_backup)
        await adopt_legacy(conn)
        await stamp_version(conn, BASELINE_VERSION)
        current = BASELINE_VERSION
        report["applied"].append("adopt-legacy")
    else:
        await adopt_legacy(conn)

    if not pending:
        await stamp_version(conn, current)
        return report

    if on_backup is not None and report["backup_path"] is None:
        report["backup_path"] = _safe_backup(on_backup)

    for migration in pending:
        log.info("Aplicando migración %d: %s", migration.version, migration.description)
        savepoint = f"mig_{migration.version}"
        await conn.execute(text(f"SAVEPOINT {savepoint}"))
        try:
            for stmt in migration.statements:
                await _execute_tolerant(conn, stmt)
            if migration.callback is not None:
                await migration.callback(conn)
            await stamp_version(conn, migration.version)
            await conn.execute(text(f"RELEASE {savepoint}"))
        except Exception:
            await conn.execute(text(f"ROLLBACK TO {savepoint}"))
            await conn.execute(text(f"RELEASE {savepoint}"))
            log.exception(
                "Migración %d falló. La BD se queda en la versión %d y la app "
                "arranca normalmente. Backup: %s",
                migration.version,
                current,
                report["backup_path"],
            )
            report["failed_at"] = migration.version
            break
        current = migration.version
        report["applied"].append(migration.version)

    report["to_version"] = current
    return report


async def _execute_tolerant(conn, stmt: str) -> None:
    try:
        await conn.execute(text(stmt))
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        head = stmt.strip().lower()
        if "no such table" in msg and (
            head.startswith("alter table") or head.startswith("create index")
        ):
            log.debug("Sentencia omitida (la tabla la crea el modelo): %s", head[:60])
            return
        raise


def _safe_backup(on_backup) -> str | None:
    try:
        path = on_backup()
        log.info("Backup previo a migración creado: %s", path)
        return str(path)
    except Exception:
        log.exception(
            "No se pudo crear el backup previo a la migración. Se continúa, "
            "pero revisa el espacio en disco y los permisos."
        )
        return None
