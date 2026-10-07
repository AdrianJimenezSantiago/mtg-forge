from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mpc_forge import config as cfg

log = logging.getLogger(__name__)


_BINARY_NAMES = [
    "autofill.exe",
    "mpc-autofill.exe",
    "mpc_autofill.exe",
    "autofill-windows.exe",
    "autofill-win.exe",
    "autofill",
    "autofill.bin",
    "autofill-macos",
    "autofill-linux",
    "mpc-autofill",
]


def _candidate_dirs() -> list[Path]:
    cands: list[Path] = []

    user_path = getattr(cfg, "MPC_AUTOFILL_EXE_PATH", "") or ""
    if user_path:
        p = Path(user_path).expanduser()
        if p.is_file():
            cands.append(p.parent)
        elif p.is_dir():
            cands.append(p)

    cwd = Path.cwd()
    cands.extend(
        [
            cwd,
            cwd / "mpc-autofill",
            cwd / "autofill",
            cwd / "tools" / "mpc-autofill",
        ]
    )

    data_dir = Path(cfg.PATHS.data_dir) if hasattr(cfg, "PATHS") else None
    if data_dir:
        cands.extend(
            [
                data_dir / "mpc-autofill",
                data_dir / "autofill",
            ]
        )

    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        if localappdata:
            cands.append(Path(localappdata) / "Programs" / "mpc-autofill")
        if userprofile:
            cands.extend(
                [
                    Path(userprofile) / "Desktop" / "mpc-autofill",
                    Path(userprofile) / "Downloads" / "mpc-autofill",
                ]
            )

    seen: set[Path] = set()
    out = []
    for p in cands:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            seen.add(rp)
            out.append(p)
    return out


@dataclass
class AutofillStatus:
    available: bool
    exe_path: str | None = None
    version: str | None = None
    source: str = "not_found"


def detect() -> AutofillStatus:
    user_path = getattr(cfg, "MPC_AUTOFILL_EXE_PATH", "") or ""
    if user_path:
        p = Path(user_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK if sys.platform != "win32" else os.F_OK):
            return AutofillStatus(True, str(p.resolve()), source="user_config")

    for name in _BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return AutofillStatus(True, found, source="path")

    for d in _candidate_dirs():
        if not d.is_dir():
            continue
        for name in _BINARY_NAMES:
            candidate = d / name
            if candidate.is_file():
                return AutofillStatus(True, str(candidate.resolve()), source="search")

    return AutofillStatus(False, None, source="not_found")


def launch(xml_path: Path) -> int:
    xml_path = xml_path.resolve()
    if not xml_path.is_file():
        raise RuntimeError(f"El XML no existe: {xml_path}")

    st = detect()
    if not st.available or not st.exe_path:
        raise RuntimeError(
            "No se encuentra el ejecutable de MPC Autofill. "
            "Descárgalo de https://github.com/chilli-axe/mpc-autofill/releases "
            "y colócalo en la carpeta del proyecto, o configura la ruta en Ajustes."
        )

    exe = Path(st.exe_path)
    cmd = [str(exe), "--directory", str(xml_path.parent)]

    log.info("Lanzando MPC Autofill: %s", " ".join(cmd))
    kwargs: dict[str, Any] = {"cwd": str(exe.parent)}
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_CONSOLE = 0x00000010
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_CONSOLE
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
    return proc.pid
