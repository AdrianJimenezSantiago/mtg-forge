"""Motor de migraciones incrementales NO destructivas.

Sustituye al esquema anterior (``SCHEMA_VERSION`` + ``drop_all`` cuando la
versión cambiaba), que borraba mazos, historial y colección del usuario en cada
cambio de esquema. Ese comportamiento se ejecutó 8 veces en la vida del
proyecto.

Reglas de este módulo
---------------------

1. **Nunca se borran datos.** No hay ``drop_all`` en ninguna ruta de código.
2. Cada migración es un escalón numerado que lleva la BD de ``N-1`` a ``N``.
3. Se aplican en orden, cada una en su propia transacción. Si una falla, se
   hace rollback de esa migración y la versión NO avanza — la app arranca con
   el esquema anterior en vez de quedarse a medias.
4. Antes de tocar nada se crea un backup automático (salvo que la BD sea nueva
   o no haya nada que aplicar).

Por qué no Alembic
------------------
Alembic es la respuesta correcta para un servicio con despliegue controlado.
Aquí distribuimos un ``.exe`` de PyInstaller: Alembic necesita que los scripts
de migración viajen como *data files*, resuelve el ``script_location`` en
runtime y arrastra su propio parser de configuración. Para un SQLite de un solo
fichero con migraciones lineales (sin ramas, sin múltiples cabezas, sin
downgrade real posible en SQLite) el coste de empaquetado no compensa. Este
módulo cubre el mismo contrato en ~200 líneas y sin dependencias nuevas.

Añadir una migración
--------------------
Apéndala al final de ``MIGRATIONS`` con ``version`` = anterior + 1. Nunca
edites ni renumeres una migración ya publicada: los usuarios que la aplicaron
tienen esa versión sellada y no volverá a ejecutarse.

Las sentencias deben ser idempotentes siempre que sea posible
(``IF NOT EXISTS``, ``add_column_if_missing``), porque una migración
interrumpida a mitad (corte de luz) puede reintentarse en el siguiente
arranque.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text

log = logging.getLogger(__name__)

# Versión del esquema tal y como quedó en el sistema antiguo, justo antes de
# introducir este motor. Cualquier BD con una versión <= BASELINE se "adopta"
# sin destruir nada: se crean las tablas que falten y se sellan a BASELINE.
BASELINE_VERSION = 8


@dataclass(frozen=True)
class Migration:
    """Un escalón del ladder.

    ``statements`` son SQL crudo. ``callback`` permite migraciones que
    necesitan lógica Python (leer filas, transformarlas, reescribirlas);
    recibe la conexión y se ejecuta después de las sentencias.
    """
    version: int
    description: str
    statements: list[str] = field(default_factory=list)
    callback: Callable[[object], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Helpers reutilizables por las migraciones
# ---------------------------------------------------------------------------

async def column_exists(conn, table: str, column: str) -> bool:
    """¿Existe la columna? SQLite no tiene ``ADD COLUMN IF NOT EXISTS``."""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return column in {row[1] for row in result}


async def table_exists(conn, table: str) -> bool:
    result = await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )
    return result.first() is not None


async def add_column_if_missing(conn, table: str, column: str, ddl: str) -> bool:
    """Añade la columna solo si falta. Devuelve True si la añadió."""
    if not await table_exists(conn, table):
        # La tabla se creará por metadata.create_all con la columna incluida.
        return False
    if await column_exists(conn, table, column):
        return False
    log.info("Migración: ADD COLUMN %s.%s", table, column)
    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return True


# ---------------------------------------------------------------------------
# Columnas que el sistema antiguo añadía en cada arranque.
#
# Se conservan aquí porque una BD adoptada desde el sistema viejo puede
# tenerlas o no según en qué versión se quedó. Se aplican durante la adopción
# a BASELINE. Para columnas NUEVAS a partir de ahora, crea una Migration.
# ---------------------------------------------------------------------------

LEGACY_COLUMNS: list[tuple[str, str, str]] = [
    ("decks", "custom_cardback_art_id",
     "INTEGER REFERENCES custom_arts(id) ON DELETE SET NULL"),
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


# ---------------------------------------------------------------------------
# EL LADDER
# ---------------------------------------------------------------------------

MIGRATIONS: list[Migration] = [
    Migration(
        version=9,
        description="Snapshots de mazo (versionado) + thumbnails de arte",
        statements=[
            # DeckSnapshot: versionado con nombre. Ver services/snapshots.py.
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
            # Ruta relativa del thumbnail WebP generado para cada arte local.
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
            # Anulables y sin default: NULL significa "aún no lo sabemos".
            # Las filas ya cacheadas se rellenan cuando Scryfall vuelva a
            # devolver esa carta (el upsert escribe todos los campos), y
            # `legalities` vacío hace que la validación caiga a "unknown",
            # que es el comportamiento actual.
            "ALTER TABLE printings ADD COLUMN price_usd FLOAT",
            "ALTER TABLE printings ADD COLUMN price_usd_foil FLOAT",
            "ALTER TABLE printings ADD COLUMN price_eur FLOAT",
            "ALTER TABLE printings ADD COLUMN legalities TEXT NOT NULL DEFAULT ''",
        ],
    ),
]

LATEST_VERSION = max([m.version for m in MIGRATIONS], default=BASELINE_VERSION)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def read_version(conn) -> int | None:
    """Versión actual del esquema, o None si la BD es nueva."""
    if not await table_exists(conn, "kv_store"):
        return None
    row = (await conn.execute(
        text("SELECT value FROM kv_store WHERE key='schema_version'")
    )).first()
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        # Versiones antiguas guardaban strings raros. Tratamos como baseline:
        # es una BD del sistema viejo, se adopta sin destruir.
        return BASELINE_VERSION


async def stamp_version(conn, version: int) -> None:
    await conn.execute(
        text("INSERT OR REPLACE INTO kv_store (key, value) "
             "VALUES ('schema_version', :v)"),
        {"v": str(version)},
    )


async def adopt_legacy(conn) -> None:
    """Adopta una BD del sistema antiguo sin borrar nada.

    El sistema viejo hacía ``drop_all`` en este punto. Aquí, en cambio,
    ``create_all`` ya ha creado las tablas que faltaban y solo hay que
    asegurar las columnas que el viejo añadía en caliente.
    """
    added = 0
    for table, column, ddl in LEGACY_COLUMNS:
        if await add_column_if_missing(conn, table, column, ddl):
            added += 1
    if added:
        log.info("Adopción legacy: %d columnas añadidas sin pérdida de datos", added)


async def run(conn, *, on_backup=None) -> dict[str, object]:
    """Aplica todas las migraciones pendientes.

    ``conn`` es una conexión async ya dentro de ``engine.begin()``, con las
    tablas de ``metadata.create_all`` ya creadas.

    ``on_backup`` es un callable síncrono opcional que crea el backup de
    seguridad. Se invoca UNA sola vez, justo antes de la primera migración
    real, y solo si la BD ya tenía datos. Devuelve la ruta del backup.
    """
    current = await read_version(conn)
    report: dict[str, object] = {
        "from_version": current,
        "to_version": LATEST_VERSION,
        "applied": [],
        "backup_path": None,
        "fresh": current is None,
    }

    if current is None:
        # BD nueva: create_all ya dejó el esquema completo y al día.
        await stamp_version(conn, LATEST_VERSION)
        log.info("BD nueva inicializada en la versión %d", LATEST_VERSION)
        return report

    if current > LATEST_VERSION:
        # El usuario ha vuelto a una versión anterior de la app. No tocamos
        # nada: SQLite tolera columnas extra, y destruir sería peor.
        log.warning(
            "La BD está en la versión %d, más nueva que la que soporta esta "
            "build (%d). Se continúa sin migrar — considera actualizar la app.",
            current, LATEST_VERSION,
        )
        return report

    pending = [m for m in MIGRATIONS if m.version > current]

    if current < BASELINE_VERSION:
        log.info(
            "BD del sistema antiguo (v%s). Adoptando a v%d sin borrar datos.",
            current, BASELINE_VERSION,
        )
        if on_backup is not None and report["backup_path"] is None:
            report["backup_path"] = _safe_backup(on_backup)
        await adopt_legacy(conn)
        await stamp_version(conn, BASELINE_VERSION)
        current = BASELINE_VERSION
        report["applied"].append("adopt-legacy")
    else:
        # Aunque estemos ya en baseline, las columnas legacy pueden faltar si
        # el usuario vino de una build intermedia. Es idempotente y barato.
        await adopt_legacy(conn)

    if not pending:
        await stamp_version(conn, current)
        return report

    if on_backup is not None and report["backup_path"] is None:
        report["backup_path"] = _safe_backup(on_backup)

    for migration in pending:
        log.info("Aplicando migración %d: %s", migration.version, migration.description)
        # SAVEPOINT: si una sentencia falla, deshacemos solo esta migración.
        # Las anteriores quedan aplicadas y selladas.
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
                migration.version, current, report["backup_path"],
            )
            report["failed_at"] = migration.version
            break
        current = migration.version
        report["applied"].append(migration.version)

    report["to_version"] = current
    return report


async def _execute_tolerant(conn, stmt: str) -> None:
    """Ejecuta una sentencia filtrando los errores que son "ya estaba hecho".

    Dos casos se toleran, y solo esos dos:

    * **duplicate column name / already exists** — un ``ALTER TABLE ... ADD
      COLUMN`` no admite ``IF NOT EXISTS``. En vez de consultar PRAGMA antes de
      cada sentencia, dejamos que falle y filtramos ese error concreto.

    * **no such table**, únicamente en ``ALTER TABLE`` y ``CREATE INDEX`` — la
      tabla no existía cuando corrió la migración porque ``create_all`` la
      creará (o ya la creó) directamente con la columna incluida. Migrar una
      tabla que el modelo va a materializar completa es un no-op legítimo.

    Cualquier otro error se propaga y dispara el rollback del savepoint.
    """
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
    """Ejecuta el callback de backup sin dejar que un fallo bloquee el arranque.

    Un backup fallido (disco lleno, permisos) es malo, pero impedir que la app
    arranque es peor: el usuario se quedaría sin acceso a sus propios datos.
    Se registra el error de forma bien visible y se continúa.
    """
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
