"""Backup y restore del estado local (BD + artes + cardbacks) a un .zip."""
from __future__ import annotations

import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from mpc_forge.config import PATHS


def create_backup(output_dir: Path | None = None) -> Path:
    """Genera un zip con la BD, los artes y los cardbacks."""
    out = output_dir or PATHS.backups_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = out / f"mpc-forge-backup-{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if PATHS.db_path.exists():
            zf.write(PATHS.db_path, arcname=f"db/{PATHS.db_path.name}")
        dirs = [
            ("art", PATHS.art_dir),
            ("custom_art", PATHS.custom_art_dir),
            ("cardbacks", PATHS.cardbacks_dir),
        ]
        for base_name, base_dir in dirs:
            for f in base_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"{base_name}/{f.relative_to(base_dir)}")
    return zip_path


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
