from __future__ import annotations

import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mpc_forge import config as cfg

MAX_TAGGED_BACKUPS = 5

MANUAL_TAG = "manual"

REGENERABLE_TABLES = frozenset({"printings", "oracle_roles", "oracle_artists", "bulk_sync_state"})
PRINTINGS_TABLE = "printings"


def create_backup(
    output_dir: Path | None = None,
    *,
    tag: str = MANUAL_TAG,
    include_art: bool = True,
) -> Path:
    out = output_dir or cfg.PATHS.backups_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    zip_path = out / f"mpc-forge-backup-{stamp}{suffix}.zip"

    if tag == "pre-migration":
        include_art = False

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if cfg.PATHS.db_path.exists():
                with tempfile.TemporaryDirectory(dir=out) as tmp:
                    copy = Path(tmp) / cfg.PATHS.db_path.name
                    copy_user_data(cfg.PATHS.db_path, copy)
                    zf.write(copy, arcname=f"db/{cfg.PATHS.db_path.name}")

            if include_art:
                dirs = [
                    ("custom_art", cfg.PATHS.custom_art_dir),
                    ("cardbacks", cfg.PATHS.cardbacks_dir),
                ]
                for base_name, base_dir in dirs:
                    for f in base_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, arcname=f"{base_name}/{f.relative_to(base_dir)}")
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise

    if tag:
        prune_tagged_backups(out, tag=tag)
    return zip_path


def copy_user_data(source: Path, target: Path) -> None:
    """Copia la BD sin los datos regenerables, en una sola lectura consistente.

    Sin las filas de bulk_sync_state la app restaurada sabe que le falta el catálogo
    y vuelve a la API de Scryfall hasta que se reimporte el volcado.
    """
    conn = sqlite3.connect(target, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("ATTACH DATABASE ? AS src", (str(source),))
        schema = conn.execute(
            "SELECT type, name, sql FROM src.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        virtual = [n for t, n, sql in schema if t == "table" and _is_virtual(sql)]
        fts = [n for t, n, sql in schema if n in virtual and "USING FTS" in sql.upper()]

        def shadow(name: str) -> bool:
            return any(name.startswith(f"{v}_") for v in virtual)

        tables = [n for t, n, sql in schema if t == "table" and not shadow(n)]
        conn.execute("BEGIN")
        for kind in ("table", "view"):
            for t, n, sql in schema:
                if t == kind and not shadow(n):
                    conn.execute(sql)
        for name in tables:
            if name in virtual or (name in REGENERABLE_TABLES and name != PRINTINGS_TABLE):
                continue
            if name == PRINTINGS_TABLE:
                _copy_referenced_printings(conn, tables)
            else:
                conn.execute(f'INSERT INTO main."{name}" SELECT * FROM src."{name}"')
        if conn.execute("SELECT 1 FROM src.sqlite_master WHERE name='sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM main.sqlite_sequence")
            conn.execute("INSERT INTO main.sqlite_sequence SELECT * FROM src.sqlite_sequence")
        for t, n, sql in schema:
            if t in ("index", "trigger") and not shadow(n):
                conn.execute(sql)
        for name in fts:
            conn.execute(f'INSERT INTO main."{name}"("{name}") VALUES (\'rebuild\')')
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")
    finally:
        conn.close()


def _is_virtual(sql: str) -> bool:
    return sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")


def _copy_referenced_printings(conn: sqlite3.Connection, tables: list[str]) -> None:
    refs = []
    for name in tables:
        if name in REGENERABLE_TABLES:
            continue
        for col in conn.execute(f'PRAGMA src.table_info("{name}")'):
            if col[1] == "scryfall_id" or col[1].endswith("_scryfall_id"):
                refs.append(f'SELECT "{col[1]}" FROM src."{name}"')
    if not refs:
        return
    conn.execute(
        f'INSERT INTO main."{PRINTINGS_TABLE}" SELECT * FROM src."{PRINTINGS_TABLE}" '
        f"WHERE scryfall_id IN ({' UNION '.join(refs)})"
    )


def prune_tagged_backups(
    directory: Path | None = None, *, tag: str, keep: int = MAX_TAGGED_BACKUPS
) -> int:
    out = directory or cfg.PATHS.backups_dir
    if not out.exists():
        return 0
    matches = sorted(
        out.glob(f"mpc-forge-backup-*-{tag}.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in matches[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def list_backups(directory: Path | None = None) -> list[dict[str, Any]]:
    out = directory or cfg.PATHS.backups_dir
    if not out.exists():
        return []
    items = []
    for f in out.glob("mpc-forge-backup-*.zip"):
        stat = f.stat()
        items.append(
            {
                "filename": f.name,
                "path": str(f),
                "bytes_size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "automatic": "-pre-migration" in f.name,
            }
        )
    items.sort(key=lambda d: str(d["created_at"]), reverse=True)
    return items
