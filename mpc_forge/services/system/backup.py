from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mpc_forge import config as cfg

MAX_TAGGED_BACKUPS = 5


def create_backup(
    output_dir: Path | None = None,
    *,
    tag: str = "",
    include_art: bool = True,
) -> Path:
    out = output_dir or cfg.PATHS.backups_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    zip_path = out / f"mpc-forge-backup-{stamp}{suffix}.zip"

    if tag == "pre-migration":
        include_art = False

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if cfg.PATHS.db_path.exists():
            zf.write(cfg.PATHS.db_path, arcname=f"db/{cfg.PATHS.db_path.name}")
        for sidecar in ("-wal", "-shm"):
            side = cfg.PATHS.db_path.with_name(cfg.PATHS.db_path.name + sidecar)
            if side.exists():
                zf.write(side, arcname=f"db/{side.name}")

        if include_art:
            dirs = [
                ("art", cfg.PATHS.art_dir),
                ("custom_art", cfg.PATHS.custom_art_dir),
                ("cardbacks", cfg.PATHS.cardbacks_dir),
            ]
            for base_name, base_dir in dirs:
                for f in base_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, arcname=f"{base_name}/{f.relative_to(base_dir)}")

    if tag:
        prune_tagged_backups(out, tag=tag)
    return zip_path


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
