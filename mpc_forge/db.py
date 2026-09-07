"""Setup de SQLAlchemy async con aiosqlite."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from mpc_forge.config import PATHS
from mpc_forge.models import Base

log = logging.getLogger(__name__)

# Sube esto cuando el schema cambie de forma incompatible.
# init_db() detecta el cambio y recrea las tablas (perdiendo datos de BD, pero
# conservando artes descargados y arte custom en disco).
SCHEMA_VERSION = "8"

DATABASE_URL = f"sqlite+aiosqlite:///{PATHS.db_path}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30.0},  # 30s de busy_timeout a nivel driver
)


# --- PRAGMAs de SQLite para concurrencia y performance --------------------
# SQLite por defecto bloquea toda la BD durante escrituras. Con estas PRAGMAs:
#   - WAL: readers y writers no se bloquean entre sí (mucho más concurrente)
#   - busy_timeout: si hay contención, espera hasta N ms antes de fallar
#     (evita "database is locked" en operaciones concurrentes)
#   - synchronous=NORMAL: seguro con WAL, más rápido que FULL
#   - cache_size=-20000: 20 MB de cache en RAM (default: 2 MB). Grande
#     porque nuestros índices y páginas hot suelen ser < 5 MB en total y
#     así casi todo cabe en memoria.
#   - temp_store=MEMORY: operaciones temporales (ORDER BY sin índice,
#     agregaciones grandes) en RAM en vez de fichero temporal en disco.
#   - mmap_size=256MB: memory-mapped I/O para las páginas leídas — evita
#     copias entre kernel y userspace. En Windows tiene menos beneficio
#     pero no perjudica.
# Esto es crítico para nuestro caso: 67 índices de drives en paralelo tocando
# la misma tabla IndexedArt, editor con múltiples fetch concurrentes, etc.
@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")           # 30 segundos
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-20000")            # 20 MB
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA mmap_size=268435456")          # 256 MB
    cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Crea todas las tablas si no existen; recrea si el schema cambió."""
    async with engine.begin() as conn:
        # ¿Existe la tabla kv_store? Si no, es primera vez → crear todo.
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='kv_store'")
        )
        kv_exists = result.first() is not None

        current_version: str | None = None
        if kv_exists:
            row = await conn.execute(
                text("SELECT value FROM kv_store WHERE key='schema_version'")
            )
            fetched = row.first()
            if fetched:
                current_version = fetched[0]

        if kv_exists and current_version != SCHEMA_VERSION:
            log.warning(
                "Schema version cambió (%s → %s). Recreando tablas — se perderán "
                "mazos e historial. Los artes descargados y el arte custom se conservan.",
                current_version, SCHEMA_VERSION,
            )
            await conn.run_sync(Base.metadata.drop_all)

        await conn.run_sync(Base.metadata.create_all)

        # --- Migraciones ligeras (ADD COLUMN idempotente) ---
        # SQLite no soporta "ADD COLUMN IF NOT EXISTS", así que primero
        # consultamos PRAGMA. Estas migraciones ocurren cuando SCHEMA_VERSION
        # NO ha cambiado (columnas nuevas retrocompatibles). Si un cambio
        # es incompatible, bumpea SCHEMA_VERSION y toca recrear.
        _add_column_if_missing = [
            # v2 PDF Studio: cardback específico del mazo (feature "cardback global").
            ("decks", "custom_cardback_art_id",
             "INTEGER REFERENCES custom_arts(id) ON DELETE SET NULL"),
            # Fase 1 · Tarea 4: tags extraídos del filename/folder de cada arte
            # indexado en un drive. `tags` es un CSV con los canonical tags
            # ("full_art,retro"); los booleanos derivados permiten filtrado
            # rápido con índice sin parsear el CSV en cada query.
            # `backfill_normalized_names()` los rellena para filas ya
            # existentes al arrancar. Ver `gdrive_indexer.extract_tags()`.
            ("indexed_art", "tags",          "VARCHAR(512) DEFAULT ''"),
            ("indexed_art", "is_full_art",   "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_borderless", "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_extended",   "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_showcase",   "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_retro",      "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_textless",   "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_promo",      "BOOLEAN DEFAULT 0"),
            ("indexed_art", "is_alt_art",    "BOOLEAN DEFAULT 0"),
            # Fase 2 · Tarea 5: metadatos canónicos extraídos de `[SET NUM]`
            # en filename o folder path. NULL = arte sin tag canónico
            # (matching por nombre sigue funcionando como antes).
            # `backfill_normalized_names()` rellena estos campos junto con
            # tags cuando NORMALIZATION_VERSION cambia.
            ("indexed_art", "expansion_code",   "VARCHAR(8) DEFAULT NULL"),
            ("indexed_art", "collector_number", "VARCHAR(16) DEFAULT NULL"),
            ("indexed_art", "canonical_source", "VARCHAR(16) DEFAULT ''"),
            # Fase 2 · Tarea 8: perceptual hash para dedupe cross-drive.
            # NULL = aún no calculado. Opt-in via setting `phash.enabled`.
            # No hay backfill automático — se rellena vía `POST /api/drives/
            # phash/compute` por source, para no gastar bandwidth sin permiso.
            ("indexed_art", "image_hash",       "VARCHAR(16) DEFAULT NULL"),
            # Extras · T7: URLs directas para tipos no-gdrive. NULL = derivar
            # con el patrón del source_type (gdrive lo hace desde file_id).
            ("indexed_art", "download_url",     "VARCHAR(1024) DEFAULT NULL"),
            ("indexed_art", "thumb_url",        "VARCHAR(1024) DEFAULT NULL"),
            # Fase 3: card_type (CARD, CARDBACK, TOKEN) determinado por carpeta.
            ("indexed_art", "card_type",        "VARCHAR(16) DEFAULT 'CARD'"),
        ]
        for table, column, ddl in _add_column_if_missing:
            info = await conn.execute(text(f"PRAGMA table_info({table})"))
            existing_cols = {row[1] for row in info}
            if column not in existing_cols:
                log.info("Migración: ADD COLUMN %s.%s", table, column)
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

        # Registramos versión actual.
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO kv_store (key, value) "
                "VALUES ('schema_version', :v)"
            ),
            {"v": SCHEMA_VERSION},
        )

        # Índices adicionales sobre columnas "hot" que SQLAlchemy no crea
        # automáticamente. En SQLite las FKs NO tienen índice implícito, así
        # que hay que crearlos a mano o los queries hacen full scan.
        # Usamos CREATE INDEX IF NOT EXISTS para que sea idempotente y no
        # requiera schema bump (se aplica sobre BD existente sin destruir).
        extra_indexes = [
            # DeckCard.deck_id: consultado en cada carga de editor, preloader, estimator
            "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_id ON deck_cards(deck_id)",
            # DeckCard.scryfall_id: consultado al invalidar cache tras cambio arte
            "CREATE INDEX IF NOT EXISTS ix_deck_cards_scryfall_id ON deck_cards(scryfall_id)",
            # DeckCard.role: filtrado en stats, preloader, xml/pdf export
            "CREATE INDEX IF NOT EXISTS ix_deck_cards_role ON deck_cards(role)",
            # Composite para el patrón más común: WHERE deck_id=? AND role IN (...)
            "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_role ON deck_cards(deck_id, role)",
            # DeckCard(deck_id, include) — usado por resolve_deck_for_xml para
            # el WHERE include=True y por búsqueda global.
            "CREATE INDEX IF NOT EXISTS ix_deck_cards_deck_include ON deck_cards(deck_id, include)",
            # DeckActivity: consulta más común es "todos los eventos de un mazo,
            # ordenados por fecha desc". Composite index (deck_id, created_at)
            # cubre exactamente ese patrón.
            "CREATE INDEX IF NOT EXISTS ix_deck_activity_deck_created ON deck_activity(deck_id, created_at DESC)",
            # Deck.updated_at: todos los listings ordenan por esto DESC
            # (list_decks, list_decks_with_activity, HTML home).
            "CREATE INDEX IF NOT EXISTS ix_decks_updated_at ON decks(updated_at DESC)",
            # PrintingCache(oracle_id, set_code, collector_number, lang):
            # localize_deck busca por esta combinación para saber si ya cacheó
            # una impresión localizada concreta.
            "CREATE INDEX IF NOT EXISTS ix_printings_locate "
            "ON printings(oracle_id, set_code, collector_number, lang)",
            # KeyValue.key ya es PK (no hace falta index extra), pero para
            # settings hacemos SELECT ... IN (?) — ver settings.get_all() /
            # set_many(). El PK ya cubre esto por scan de índice.
            # Fase 1 · Tarea 4: composite index sobre (name_normalized, is_*)
            # NO es óptimo porque los flags cambian mucho el plan de query.
            # Preferimos partial indexes: uno por flag, solo sobre filas donde
            # el flag es true (la mayoría son false, así los índices son
            # pequeños y las queries "is_full_art=1 AND name_normalized LIKE ?"
            # atacan primero al índice pequeño).
            # PARTIAL INDEX es soportado en SQLite desde 3.8.0.
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
            # Fase 1 · Tarea 2: la tabla dfc_pairs se consulta principalmente
            # con LOWER(front_name)=?. El UNIQUE en front_name ya da un índice
            # case-sensitive; añadimos uno lower para acelerar los lookups
            # sin depender de la comparación case-insensitive.
            "CREATE INDEX IF NOT EXISTS ix_dfc_pairs_front_lower "
            "ON dfc_pairs(lower(front_name))",
            # Fase 2 · Tarea 5: filtro por set canónico (`expansion_code`) en
            # el picker de drives. La columna guarda NULL para la mayoría de
            # filas → index parcial ahorra espacio y acelera queries.
            "CREATE INDEX IF NOT EXISTS ix_indexed_art_expansion "
            "ON indexed_art(expansion_code) WHERE expansion_code IS NOT NULL",
            # Fase 2 · Tarea 8: index parcial sobre image_hash — la mayoría
            # de filas serán NULL hasta que el usuario active pHash. El
            # index acelera `find_similar()` que hace WHERE image_hash IS
            # NOT NULL antes del scan hamming en memoria.
            "CREATE INDEX IF NOT EXISTS ix_indexed_art_phash "
            "ON indexed_art(image_hash) WHERE image_hash IS NOT NULL",
            # Fase 3: card_type para filtrar cardbacks/tokens rápidamente
            "CREATE INDEX IF NOT EXISTS ix_indexed_art_card_type "
            "ON indexed_art(card_type)",
        ]
        for stmt in extra_indexes:
            await conn.execute(text(stmt))

        # --- FTS5 para búsqueda en drives (Fase 2 · Tarea 6) ---
        # SQLite 3.9+ trae FTS5 (search full-text con ranking BM25). Comprobamos
        # que la extensión está disponible antes de usarla. Si no lo está
        # (raro: builds antiguas de Windows con SQLite estático sin FTS5), la
        # búsqueda cae a la implementación LIKE anterior — 100% transparente
        # para el resto de la app.
        #
        # La tabla virtual es una "sombra" que sincronizamos vía triggers con
        # `indexed_art`. Guardamos ahí solo los campos consultados por la UI
        # de búsqueda (name_normalized + filename + tags), no toda la fila.
        # `content=` la enlaza a `indexed_art` como "tabla contenido externo":
        # FTS5 solo mantiene el índice invertido, no una copia de los datos.
        # Rowid compartido → JOINs triviales.
        fts5_ok = await _try_setup_fts5(conn)
        # Guardamos el flag en KV para que el runtime sepa si puede usar MATCH.
        # Ver `gdrive_search.search()`.
        await conn.execute(text(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES ('fts5_available', :v)"
        ), {"v": "1" if fts5_ok else "0"})


async def _try_setup_fts5(conn) -> bool:
    """Intenta crear la tabla virtual FTS5 y sus triggers. Devuelve True si
    FTS5 está disponible y todo se aplicó bien.

    Idempotente: usa CREATE VIRTUAL TABLE IF NOT EXISTS y triggers WHERE.
    Si FTS5 no está compilado en la SQLite del usuario, el primer CREATE
    lanza una excepción OperationalError; la capturamos y devolvemos False.
    """
    try:
        # Detección: intentamos crear una tabla virtual efímera. Si FTS5 no
        # está compilado, SQLite tira "no such module: fts5" aquí.
        await conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS indexed_art_fts USING fts5("
            "  name_normalized,"
            "  filename,"
            "  tags,"
            # `content=` externo apunta a la tabla real. FTS5 solo mantiene
            # el índice invertido — cero duplicación de datos.
            "  content='indexed_art',"
            "  content_rowid='id',"
            # Tokenizer: unicode61 con remove_diacritics=2 hace asciifolding
            # nativo dentro de FTS5. Complementario al que aplicamos al
            # indexar (defense in depth: si el name_normalized ya está
            # asciifolded, FTS5 no toca; si no, FTS5 lo hace).
            "  tokenize='unicode61 remove_diacritics 2'"
            ")"
        ))
    except Exception as e:  # noqa: BLE001
        log.warning(
            "FTS5 no disponible en esta build de SQLite (%s). La búsqueda usará "
            "el modo LIKE. Actualiza el runtime si tienes muchos drives — "
            "FTS5 acelera 10-100x las queries.", e
        )
        return False

    # Triggers de sincronización. Idempotentes: si ya existen, no hacen nada
    # (CREATE TRIGGER IF NOT EXISTS soportado desde SQLite 3.7).
    await conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_insert "
        "AFTER INSERT ON indexed_art BEGIN "
        "  INSERT INTO indexed_art_fts(rowid, name_normalized, filename, tags) "
        "  VALUES (new.id, new.name_normalized, new.filename, new.tags); "
        "END"
    ))
    await conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_delete "
        "AFTER DELETE ON indexed_art BEGIN "
        "  INSERT INTO indexed_art_fts(indexed_art_fts, rowid, name_normalized, filename, tags) "
        "  VALUES ('delete', old.id, old.name_normalized, old.filename, old.tags); "
        "END"
    ))
    await conn.execute(text(
        "CREATE TRIGGER IF NOT EXISTS indexed_art_fts_update "
        "AFTER UPDATE ON indexed_art BEGIN "
        "  INSERT INTO indexed_art_fts(indexed_art_fts, rowid, name_normalized, filename, tags) "
        "  VALUES ('delete', old.id, old.name_normalized, old.filename, old.tags); "
        "  INSERT INTO indexed_art_fts(rowid, name_normalized, filename, tags) "
        "  VALUES (new.id, new.name_normalized, new.filename, new.tags); "
        "END"
    ))

    # Si el índice está vacío pero indexed_art tiene filas, rebuild manual.
    # Comando FTS5 documentado: INSERT INTO fts(fts, cmd) VALUES ('rebuild');
    # Solo se ejecuta cuando la tabla virtual está vacía Y la real no —
    # típicamente después de un upgrade que introduce FTS5.
    real_count = int((await conn.execute(text(
        "SELECT COUNT(*) FROM indexed_art"
    ))).scalar() or 0)
    fts_count = int((await conn.execute(text(
        "SELECT COUNT(*) FROM indexed_art_fts"
    ))).scalar() or 0)
    if real_count > 0 and fts_count == 0:
        log.info("FTS5 vacío pero %d filas en indexed_art — rebuild de la tabla virtual…",
                 real_count)
        await conn.execute(text(
            "INSERT INTO indexed_art_fts(indexed_art_fts) VALUES ('rebuild')"
        ))
        log.info("Rebuild de FTS5 completo")

    return True


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI para inyección de sesión."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Contexto manual para scripts o servicios fuera del ciclo request/response."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
