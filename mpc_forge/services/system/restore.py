from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from mpc_forge import config as cfg
from mpc_forge.errors import CodedError
from mpc_forge.services.system import backup as backup_service

log = logging.getLogger(__name__)

PRE_RESTORE_TAG = "pre-restore"
UPLOADED_TAG = "uploaded"
_ART_FOLDERS = ("custom_art", "cardbacks")
_COUNTED_TABLES = {
    "decks": "decks",
    "deck_cards": "cards",
    "collection_entries": "collection",
    "custom_arts": "custom_arts",
    "deck_snapshots": "snapshots",
}


class RestoreError(CodedError):
    code = "restore_failed"


@dataclass
class BackupSummary:
    filename: str
    created_at: str
    bytes_size: int
    schema_version: int | None
    decks: int = 0
    cards: int = 0
    collection: int = 0
    custom_arts: int = 0
    snapshots: int = 0
    art_files: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def resolve_backup(filename: str, directory: Path | None = None) -> Path:
    base = (directory or cfg.PATHS.backups_dir).resolve()
    name = Path(filename).name
    if name != filename or not name.startswith("mpc-forge-backup-") or not name.endswith(".zip"):
        raise RestoreError(code="backup_not_found", filename=filename)
    path = (base / name).resolve()
    if path.parent != base or not path.is_file():
        raise RestoreError(code="backup_not_found", filename=filename)
    return path


def _db_member(zf: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [
        m for m in zf.infolist() if m.filename.startswith("db/") and m.filename.endswith(".sqlite3")
    ]
    if len(members) != 1:
        raise RestoreError(code="backup_invalid")
    return members[0]


def _safe_art_target(member: str) -> Path | None:
    parts = PurePosixPath(member).parts
    if len(parts) < 2 or parts[0] not in _ART_FOLDERS or member.endswith("/"):
        return None
    if any(p in ("", ".", "..") for p in parts) or "\\" in member or ":" in member:
        return None
    base = cfg.PATHS.custom_art_dir if parts[0] == "custom_art" else cfg.PATHS.cardbacks_dir
    target = base.joinpath(*parts[1:]).resolve()
    if not target.is_relative_to(base.resolve()):
        return None
    return target


def _validate_database(path: Path) -> dict[str, int | None]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise RestoreError(code="backup_invalid") from e
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RestoreError(code="backup_corrupt")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"decks", "deck_cards", "kv_store"} <= tables:
            raise RestoreError(code="backup_invalid")
        counts: dict[str, int | None] = {}
        for table, label in _COUNTED_TABLES.items():
            counts[label] = (
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if table in tables
                else 0
            )
        row = conn.execute("SELECT value FROM kv_store WHERE key='schema_version'").fetchone()
        try:
            counts["schema_version"] = int(row[0]) if row else None
        except (TypeError, ValueError):
            counts["schema_version"] = None
        return counts
    except sqlite3.DatabaseError as e:
        raise RestoreError(code="backup_corrupt") from e
    finally:
        conn.close()


def _extract_db(zf: zipfile.ZipFile, target_dir: Path) -> Path:
    member = _db_member(zf)
    out = target_dir / "restore.sqlite3"
    with zf.open(member) as src, out.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


def inspect(path: Path) -> BackupSummary:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise RestoreError(code="backup_invalid") from e
    with zf, tempfile.TemporaryDirectory() as tmp:
        counts = _validate_database(_extract_db(zf, Path(tmp)))
        art_files = sum(1 for m in zf.infolist() if _safe_art_target(m.filename))
    stat = path.stat()
    return BackupSummary(
        filename=path.name,
        created_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        bytes_size=stat.st_size,
        schema_version=counts.pop("schema_version"),
        art_files=art_files,
        **{k: int(v or 0) for k, v in counts.items()},
    )


@dataclass
class RestoreResult:
    summary: BackupSummary
    safety_backup: str
    restored_art_files: int


def restore_files(path: Path) -> RestoreResult:
    """Sustituye la BD y repone el arte propio y los reversos del backup.

    La BD se copia con la API de backup de SQLite sobre el fichero en uso, así
    que funciona con la app abierta (también en Windows) y respeta el WAL. Antes
    se guarda un backup de seguridad del estado actual.
    """
    summary = inspect(path)
    if summary.schema_version and summary.schema_version > _latest_version():
        raise RestoreError(code="backup_too_new", version=summary.schema_version)
    safety = backup_service.create_backup(tag=PRE_RESTORE_TAG)

    restored_files = 0
    with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory() as tmp:
        db_copy = _extract_db(zf, Path(tmp))
        src = sqlite3.connect(db_copy)
        dst = sqlite3.connect(cfg.PATHS.db_path, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        for member in zf.infolist():
            target = _safe_art_target(member.filename)
            if target is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as s, target.open("wb") as d:
                shutil.copyfileobj(s, d)
            restored_files += 1
    log.info(
        "Backup restaurado desde %s (%d ficheros de arte). Copia previa: %s",
        path.name,
        restored_files,
        safety.name,
    )
    return RestoreResult(
        summary=summary, safety_backup=safety.name, restored_art_files=restored_files
    )


def save_upload(chunks_path: Path) -> Path:
    out = cfg.PATHS.backups_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = out / f"mpc-forge-backup-{stamp}-{UPLOADED_TAG}.zip"
    shutil.move(str(chunks_path), target)
    backup_service.prune_tagged_backups(out, tag=UPLOADED_TAG)
    return target


def delete_backup(filename: str) -> None:
    resolve_backup(filename).unlink()


def _latest_version() -> int:
    from mpc_forge import migrations

    return migrations.LATEST_VERSION
