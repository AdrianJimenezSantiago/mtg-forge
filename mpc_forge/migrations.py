from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from mpc_forge.services.cards import images

log = logging.getLogger(__name__)

BASELINE_VERSION = 8


def _webp_statements() -> list[str]:
    columns = {
        "printings": {
            "image_normal": "normal",
            "back_image_normal": "normal",
            "image_large": "large",
            "back_image_large": "large",
        },
        "collection_entries": {"image_small": "small"},
    }
    out = []
    for table, cols in columns.items():
        sets, legacy = [], []
        for col, old in cols.items():
            prefix = f"https://cards.scryfall.io/{old}/"
            webp = f"https://cards.scryfall.io/{images.WEBP_FOR[old]}/"
            match = f"{col} LIKE '{prefix}%.jpg%'"
            sets.append(
                f"{col} = CASE WHEN {match} THEN replace(replace({col}, '{prefix}', '{webp}'), "
                f"'.jpg', '.webp') ELSE {col} END"
            )
            legacy.append(match)
        out.append(f"UPDATE {table} SET {', '.join(sets)} WHERE {' OR '.join(legacy)}")
    return out


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    statements: list[str] = field(default_factory=list)
    callback: Callable[[object], Awaitable[None]] | None = None


async def column_exists(conn: AsyncConnection, table: str, column: str) -> bool:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return column in {row[1] for row in result}


async def table_exists(conn: AsyncConnection, table: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )
    return result.first() is not None


async def add_column_if_missing(conn: AsyncConnection, table: str, column: str, ddl: str) -> bool:
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
    Migration(
        version=13,
        description="Elimina índices duplicados o cubiertos por índices compuestos",
        statements=[
            "DROP INDEX IF EXISTS ix_deck_cards_deck_id",
            "DROP INDEX IF EXISTS ix_collection_set",
            "DROP INDEX IF EXISTS ix_collection_oracle",
            "DROP INDEX IF EXISTS ix_printings_oracle_id",
            "DROP INDEX IF EXISTS ix_deck_activity_deck_id",
        ],
    ),
    Migration(
        version=14,
        description="Marca de impresión solo digital en printings",
        statements=[
            "ALTER TABLE printings ADD COLUMN digital BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    Migration(
        version=15,
        description="Imágenes de Scryfall en WebP (los JPEG small/large se retiran en 2027)",
        statements=_webp_statements(),
    ),
    Migration(
        version=16,
        description="Tipo de set, efectos de marco, estado de imagen e ilustración en printings",
        statements=[
            "ALTER TABLE printings ADD COLUMN set_type VARCHAR(32) NOT NULL DEFAULT ''",
            "ALTER TABLE printings ADD COLUMN frame_effects VARCHAR(256) NOT NULL DEFAULT ''",
            "ALTER TABLE printings ADD COLUMN image_status VARCHAR(16) NOT NULL DEFAULT ''",
            "ALTER TABLE printings ADD COLUMN image_updated_at VARCHAR(32)",
            "ALTER TABLE printings ADD COLUMN illustration_id VARCHAR(64)",
        ],
    ),
    Migration(
        version=17,
        description="Funciones de cada carta (ramp, robo, removal…) según las etiquetas de Scryfall",
        statements=[
            """
            CREATE TABLE IF NOT EXISTS oracle_roles (
                oracle_id VARCHAR(64) NOT NULL,
                role VARCHAR(32) NOT NULL,
                PRIMARY KEY (oracle_id, role)
            )
            """,
        ],
    ),
    Migration(
        version=18,
        description="Carpetas de arte custom vinculadas fuera de la carpeta de la app",
        statements=[
            """
            CREATE TABLE IF NOT EXISTS custom_art_folders (
                id INTEGER NOT NULL PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                label VARCHAR(256) NOT NULL DEFAULT '',
                file_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                last_scanned_at DATETIME
            )
            """,
        ],
    ),
    Migration(
        version=19,
        description="Etiquetas de mazo para organizar la biblioteca",
        statements=["ALTER TABLE decks ADD COLUMN tags TEXT NOT NULL DEFAULT ''"],
    ),
    Migration(
        version=20,
        description="Cantidades normales y foil de cada impresión de la colección",
        statements=[
            "ALTER TABLE collection_entries ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE collection_entries ADD COLUMN foil_quantity INTEGER NOT NULL DEFAULT 0",
        ],
    ),
    Migration(
        version=21,
        description="Dimensiones del arte custom para avisar de la baja resolución",
        statements=[
            "ALTER TABLE custom_arts ADD COLUMN width INTEGER",
            "ALTER TABLE custom_arts ADD COLUMN height INTEGER",
        ],
    ),
]

LATEST_VERSION = max([m.version for m in MIGRATIONS], default=BASELINE_VERSION)


async def read_version(conn: AsyncConnection) -> int | None:
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


async def stamp_version(conn: AsyncConnection, version: int) -> None:
    await conn.execute(
        text("INSERT OR REPLACE INTO kv_store (key, value) VALUES ('schema_version', :v)"),
        {"v": str(version)},
    )


async def adopt_legacy(conn: AsyncConnection) -> None:
    added = 0
    for table, column, ddl in LEGACY_COLUMNS:
        if await add_column_if_missing(conn, table, column, ddl):
            added += 1
    if added:
        log.info("Adopción legacy: %d columnas añadidas sin pérdida de datos", added)


async def run(
    conn: AsyncConnection, *, on_backup: Callable[[], object] | None = None
) -> dict[str, object]:
    current = await read_version(conn)
    applied: list[int | str] = []
    report: dict[str, object] = {
        "from_version": current,
        "to_version": LATEST_VERSION,
        "applied": applied,
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
        applied.append("adopt-legacy")
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
        applied.append(migration.version)

    report["to_version"] = current
    return report


async def _execute_tolerant(conn: AsyncConnection, stmt: str) -> None:
    try:
        await conn.execute(text(stmt))
    except Exception as e:
        msg = str(e).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        head = stmt.strip().lower()
        if "no such table" in msg and head.startswith(("alter table", "create index", "update ")):
            log.debug("Sentencia omitida (la tabla la crea el modelo): %s", head[:60])
            return
        raise


def _safe_backup(on_backup: Callable[[], object]) -> str | None:
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
