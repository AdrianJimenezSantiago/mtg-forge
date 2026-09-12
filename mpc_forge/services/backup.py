"""Backup y restore del estado local (BD + artes + cardbacks) a un .zip."""
from __future__ import annotations

import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from mpc_forge.config import PATHS

# Cuántos backups automáticos (los que llevan tag) se conservan. Los backups
# manuales del usuario nunca se podan.
MAX_TAGGED_BACKUPS = 5


def create_backup(
    output_dir: Path | None = None,
    *,
    tag: str = "",
    include_art: bool = True,
) -> Path:
    """Genera un zip con la BD, los artes y los cardbacks.

    ``tag`` marca el backup como automático (p.ej. ``"pre-migration"``) y lo
    incluye en el nombre del fichero para que el usuario entienda de dónde
    salió. Los backups con tag se podan automáticamente.

    ``include_art=False`` produce un backup solo-BD: es lo que queremos antes
    de una migración, porque las imágenes no las toca ninguna migración y
    copiar decenas de GB en cada arranque haría el proceso inviable.
    """
    out = output_dir or PATHS.backups_dir
    out.mkdir(parents=True, exist_ok=True)
    # Hora local: el nombre del backup lo lee una persona en su carpeta.
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    zip_path = out / f"mpc-forge-backup-{stamp}{suffix}.zip"

    # Antes de una migración solo hace falta la BD: es lo único que la
    # migración puede dañar, y es lo único irrecuperable (los artes se
    # vuelven a descargar, un mazo perdido no vuelve).
    if tag == "pre-migration":
        include_art = False

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if PATHS.db_path.exists():
            zf.write(PATHS.db_path, arcname=f"db/{PATHS.db_path.name}")
        # El WAL puede contener transacciones aún no volcadas al .sqlite3.
        # Sin él, restaurar el backup perdería los últimos cambios.
        for sidecar in ("-wal", "-shm"):
            side = PATHS.db_path.with_name(PATHS.db_path.name + sidecar)
            if side.exists():
                zf.write(side, arcname=f"db/{side.name}")

        if include_art:
            dirs = [
                ("art", PATHS.art_dir),
                ("custom_art", PATHS.custom_art_dir),
                ("cardbacks", PATHS.cardbacks_dir),
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
    """Borra los backups automáticos más antiguos con ese tag.

    Sin esto, un usuario que actualice la app muchas veces acumularía un zip
    por migración indefinidamente. Devuelve cuántos se borraron.
    """
    out = directory or PATHS.backups_dir
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
            # Un backup que no se puede borrar (bloqueado por el antivirus,
            # permisos) no es motivo para romper nada. Se reintentará.
            pass
    return removed


def list_backups(directory: Path | None = None) -> list[dict]:
    """Lista los backups disponibles, del más reciente al más antiguo."""
    out = directory or PATHS.backups_dir
    if not out.exists():
        return []
    items = []
    for f in out.glob("mpc-forge-backup-*.zip"):
        stat = f.stat()
        items.append({
            "filename": f.name,
            "path": str(f),
            "bytes_size": stat.st_size,
            "created_at": datetime.fromtimestamp(
                stat.st_mtime, tz=UTC
            ).isoformat(),
            "automatic": "-pre-migration" in f.name,
        })
    items.sort(key=lambda d: d["created_at"], reverse=True)
    return items


def restore_backup(zip_path: Path) -> None:
    """Restaura un backup. **Sobrescribe** el estado actual."""
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)
    # Limpiamos directorios (peligroso — el caller debe confirmar):
    for d in (PATHS.art_dir, PATHS.custom_art_dir, PATHS.cardbacks_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    if PATHS.db_path.exists():
        PATHS.db_path.unlink()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("db/"):
                target = PATHS.db_path
            elif name.startswith("art/"):
                target = PATHS.art_dir / name[len("art/"):]
            elif name.startswith("custom_art/"):
                target = PATHS.custom_art_dir / name[len("custom_art/"):]
            elif name.startswith("cardbacks/"):
                target = PATHS.cardbacks_dir / name[len("cardbacks/"):]
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
