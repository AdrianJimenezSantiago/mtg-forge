from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mpc_forge import config as cfg

log = logging.getLogger(__name__)

Kind = Literal["essential", "refetchable", "derived", "output", "safety"]

CACHE_TTL_SECONDS = 60.0

PURGE_TARGETS: frozenset[str] = frozenset({"thumbs", "exports", "logs", "backups"})

DEFAULT_KEEP_BACKUPS = 1


@dataclass(frozen=True)
class Category:
    key: str
    kind: Kind
    purge_target: str | None = None


CATEGORIES: tuple[Category, ...] = (
    Category("database", "essential"),
    Category("art", "refetchable"),
    Category("custom_art", "essential"),
    Category("cardbacks", "essential"),
    Category("thumbs", "derived", purge_target="thumbs"),
    Category("exports", "output", purge_target="exports"),
    Category("backups", "safety", purge_target="backups"),
    Category("logs", "derived", purge_target="logs"),
    Category("other", "essential"),
)

_cache: tuple[float, dict[str, Any]] | None = None


def _logs_dir() -> Path:
    return cfg.PATHS.data_dir / "logs"


def category_paths() -> dict[str, Path]:
    p = cfg.PATHS
    return {
        "database": p.db_path,
        "art": p.art_dir,
        "custom_art": p.custom_art_dir,
        "cardbacks": p.cardbacks_dir,
        "thumbs": p.thumbs_dir,
        "exports": p.exports_dir,
        "backups": p.backups_dir,
        "logs": _logs_dir(),
        "other": p.data_dir,
    }


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _scan_dir(root: Path, *, exclude: frozenset[Path] = frozenset()) -> tuple[int, int]:
    files = 0
    total = 0
    stack: list[Path] = [root]
    seen: set[Path] = set()
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = _resolved(Path(entry.path))
                            if child in exclude or child in seen:
                                continue
                            seen.add(child)
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, total


def _scan_database() -> tuple[int, int]:
    files = 0
    total = 0
    db = cfg.PATHS.db_path
    for candidate in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            total += candidate.stat().st_size
            files += 1
        except OSError:
            continue
    return files, total


def _scan_other(exclude: frozenset[Path]) -> tuple[int, int]:
    root = cfg.PATHS.data_dir
    db = cfg.PATHS.db_path
    db_files = {
        _resolved(db),
        _resolved(db.with_name(db.name + "-wal")),
        _resolved(db.with_name(db.name + "-shm")),
    }
    files = 0
    total = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            resolved = _resolved(Path(entry.path))
            if resolved in exclude or resolved in db_files:
                continue
            if entry.is_dir(follow_symlinks=False):
                sub_files, sub_bytes = _scan_dir(Path(entry.path), exclude=exclude)
                files += sub_files
                total += sub_bytes
            elif entry.is_file(follow_symlinks=False):
                files += 1
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return files, total


def _mount_point(path: Path) -> Path:
    current = _resolved(path)
    while True:
        try:
            if os.path.ismount(current):
                return current
        except OSError:
            break
        if current.parent == current:
            break
        current = current.parent
    return Path(current.anchor or current)


def _backups_summary() -> dict[str, Any]:
    from mpc_forge.services.system import backup as backup_service

    try:
        items = backup_service.list_backups(cfg.PATHS.backups_dir)
    except OSError:
        return {"count": 0, "automatic": 0, "manual": 0, "bytes": 0, "latest_at": None}
    automatic = sum(1 for b in items if b["automatic"])
    return {
        "count": len(items),
        "automatic": automatic,
        "manual": len(items) - automatic,
        "bytes": sum(int(b["bytes_size"]) for b in items),
        "latest_at": items[0]["created_at"] if items else None,
    }


def compute() -> dict[str, Any]:
    started = time.monotonic()
    paths = category_paths()
    resolved = {key: _resolved(p) for key, p in paths.items()}

    all_roots = frozenset(resolved.values())

    categories: list[dict[str, Any]] = []
    for cat in CATEGORIES:
        path = paths[cat.key]
        exclude = frozenset(all_roots - {resolved[cat.key]})
        if cat.key == "database":
            files, size = _scan_database()
            exists = cfg.PATHS.db_path.exists()
        elif cat.key == "other":
            files, size = _scan_other(exclude)
            exists = path.exists()
        else:
            exists = path.is_dir()
            files, size = _scan_dir(path, exclude=exclude) if exists else (0, 0)
        categories.append(
            {
                "key": cat.key,
                "kind": cat.kind,
                "path": str(path),
                "exists": exists,
                "files": files,
                "bytes": size,
                "purge_target": cat.purge_target,
                "reclaimable": cat.purge_target is not None,
            }
        )

    total_bytes = sum(c["bytes"] for c in categories)
    by_kind: dict[str, int] = {}
    for c in categories:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + c["bytes"]

    volumes: dict[str, dict[str, Any]] = {}
    for c in categories:
        if not c["exists"]:
            continue
        mount = str(_mount_point(Path(c["path"])))
        vol = volumes.get(mount)
        if vol is None:
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            vol = volumes[mount] = {
                "mount": mount,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "app_bytes": 0,
                "categories": [],
            }
        vol["app_bytes"] += c["bytes"]
        vol["categories"].append(c["key"])

    sizes = {c["key"]: c["bytes"] for c in categories}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scan_seconds": round(time.monotonic() - started, 3),
        "data_dir": str(cfg.PATHS.data_dir),
        "categories": categories,
        "totals": {
            "bytes": total_bytes,
            "files": sum(c["files"] for c in categories),
            "reclaimable_bytes": sum(c["bytes"] for c in categories if c["reclaimable"]),
            "by_kind": by_kind,
        },
        "volumes": sorted(volumes.values(), key=lambda v: -v["app_bytes"]),
        "backups": _backups_summary(),
        "backup_estimate": {
            "full_bytes": (
                sizes["database"] + sizes["art"] + sizes["custom_art"] + sizes["cardbacks"]
            ),
            "db_only_bytes": sizes["database"],
        },
    }


async def snapshot(*, refresh: bool = False) -> dict[str, Any]:
    global _cache
    if not refresh and _cache is not None:
        age = time.monotonic() - _cache[0]
        if age < CACHE_TTL_SECONDS:
            return {**_cache[1], "cached": True, "age_seconds": round(age, 1)}
    data = await asyncio.to_thread(compute)
    _cache = (time.monotonic(), data)
    return {**data, "cached": False, "age_seconds": 0.0}


def peek() -> dict[str, Any] | None:
    if _cache is None:
        return None
    return {**_cache[1], "cached": True}


def invalidate() -> None:
    global _cache
    _cache = None


def _delete_files(paths: list[Path]) -> tuple[int, int]:
    removed = 0
    freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


def _purge_thumbs() -> tuple[int, int]:
    directory = cfg.PATHS.thumbs_dir
    if not directory.is_dir():
        return 0, 0
    return _delete_files([f for f in directory.rglob("*.webp") if f.is_file()])


def _purge_exports(older_than_days: int) -> tuple[int, int]:
    directory = cfg.PATHS.exports_dir
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - older_than_days * 86400 if older_than_days > 0 else None
    victims = []
    for f in directory.rglob("*"):
        try:
            if not f.is_file():
                continue
            if cutoff is not None and f.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        victims.append(f)
    return _delete_files(victims)


def _purge_logs() -> tuple[int, int]:
    from mpc_forge.services.system import logging_setup

    directory = _logs_dir()
    if not directory.is_dir():
        return 0, 0
    active = logging_setup.current_log_path()
    active_resolved = _resolved(active) if active is not None else None
    victims = [f for f in directory.iterdir() if f.is_file() and _resolved(f) != active_resolved]
    return _delete_files(victims)


def _purge_backups(keep: int) -> tuple[int, int]:
    from mpc_forge.services.system import backup as backup_service

    directory = cfg.PATHS.backups_dir
    before = {b["path"]: int(b["bytes_size"]) for b in backup_service.list_backups(directory)}
    backup_service.prune_tagged_backups(directory, tag="pre-migration", keep=max(0, keep))
    after = {b["path"] for b in backup_service.list_backups(directory)}
    gone = [p for p in before if p not in after]
    return len(gone), sum(before[p] for p in gone)


def purge(
    targets: list[str] | tuple[str, ...],
    *,
    exports_older_than_days: int = 0,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
) -> dict[str, Any]:
    unknown = sorted(set(targets) - PURGE_TARGETS)
    if unknown:
        raise ValueError(f"Objetivos no purgables: {', '.join(unknown)}")

    results: dict[str, dict[str, int]] = {}
    for target in ("thumbs", "exports", "logs", "backups"):
        if target not in targets:
            continue
        if target == "thumbs":
            removed, freed = _purge_thumbs()
        elif target == "exports":
            removed, freed = _purge_exports(max(0, exports_older_than_days))
        elif target == "logs":
            removed, freed = _purge_logs()
        else:
            removed, freed = _purge_backups(keep_backups)
        results[target] = {"removed": removed, "freed_bytes": freed}

    invalidate()
    total_freed = sum(r["freed_bytes"] for r in results.values())
    log.info(
        "Limpieza de almacenamiento (%s): %d ficheros, %.1f MB liberados",
        ", ".join(sorted(results)) or "nada",
        sum(r["removed"] for r in results.values()),
        total_freed / (1024 * 1024),
    )
    return {
        "targets": results,
        "removed": sum(r["removed"] for r in results.values()),
        "freed_bytes": total_freed,
    }
