"""Integración con el desktop tool de MPC Autofill (github.com/chilli-axe/mpc-autofill).

Flujo del usuario:
1. Descarga el binario desde https://github.com/chilli-axe/mpc-autofill/releases
2. Lo coloca en el PATH, en la carpeta del proyecto, o configura la ruta en Settings.
3. Genera el XML en MPC Forge.
4. Pulsa "Enviar a MPC Autofill" → nuestra app lanza el binario con el XML.

El binario recibe un XML como argumento (o vía --directory apuntando a la carpeta con el XML)
y automatiza toda la subida a MakePlayingCards.com.

Ver: https://github.com/chilli-axe/mpc-autofill/wiki/Desktop-Tool
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mpc_forge import config as cfg

log = logging.getLogger(__name__)


# Nombres típicos del binario según plataforma y versión
_BINARY_NAMES = [
    # Windows
    "autofill.exe",
    "mpc-autofill.exe",
    "mpc_autofill.exe",
    "autofill-windows.exe",
    "autofill-win.exe",
    # macOS / Linux (compilado con Nuitka o Pyinstaller)
    "autofill",
    "autofill.bin",
    "autofill-macos",
    "autofill-linux",
    "mpc-autofill",
]


# Directorios donde buscar el ejecutable
def _candidate_dirs() -> list[Path]:
    """Directorios donde probar a encontrar el binario, en orden de prioridad."""
    cands: list[Path] = []

    # 1) Ruta explícita configurada por el usuario en Settings
    user_path = getattr(cfg, "MPC_AUTOFILL_EXE_PATH", "") or ""
    if user_path:
        p = Path(user_path).expanduser()
        if p.is_file():
            cands.append(p.parent)
        elif p.is_dir():
            cands.append(p)

    # 2) Carpeta del proyecto (donde el usuario ejecuta la app)
    cwd = Path.cwd()
    cands.extend([
        cwd,
        cwd / "mpc-autofill",
        cwd / "autofill",
        cwd / "tools" / "mpc-autofill",
    ])

    # 3) Carpeta de datos de MPC Forge (por si el usuario lo pone allí)
    data_dir = Path(cfg.PATHS.data_dir) if hasattr(cfg, "PATHS") else None
    if data_dir:
        cands.extend([
            data_dir / "mpc-autofill",
            data_dir / "autofill",
        ])

    # 4) Ubicaciones comunes en Windows
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        if localappdata:
            cands.append(Path(localappdata) / "Programs" / "mpc-autofill")
        if userprofile:
            cands.extend([
                Path(userprofile) / "Desktop" / "mpc-autofill",
                Path(userprofile) / "Downloads" / "mpc-autofill",
            ])

    # Dedup manteniendo orden
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
    source: str = "not_found"  # "user_config" | "path" | "cwd" | "search" | "not_found"


def detect() -> AutofillStatus:
    """Localiza el binario. Orden:
    1. Ruta explícita del usuario (setting)
    2. PATH del sistema (shutil.which)
    3. Directorios candidatos (project cwd, %LOCALAPPDATA%, etc.)

    NO ejecuta nada — solo comprueba que el archivo existe y es ejecutable.
    """
    # 1) Setting explícito
    user_path = getattr(cfg, "MPC_AUTOFILL_EXE_PATH", "") or ""
    if user_path:
        p = Path(user_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK if sys.platform != "win32" else os.F_OK):
            return AutofillStatus(True, str(p.resolve()), source="user_config")

    # 2) PATH
    for name in _BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return AutofillStatus(True, found, source="path")

    # 3) Búsqueda en directorios candidatos
    for d in _candidate_dirs():
        if not d.is_dir():
            continue
        for name in _BINARY_NAMES:
            candidate = d / name
            if candidate.is_file():
                return AutofillStatus(True, str(candidate.resolve()), source="search")

    return AutofillStatus(False, None, source="not_found")


def launch(xml_path: Path) -> int:
    """Lanza el desktop tool con el XML dado. Devuelve el PID del proceso.

    Corre en background — no bloqueamos la respuesta HTTP. El usuario ve la
    ventana del autofill abrirse y desde ahí sigue el flujo normal (browser
    automation con Selenium).

    Lanza RuntimeError si no encuentra el binario o si el XML no existe.
    """
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

    # El desktop tool acepta --directory apuntando a la carpeta con el XML.
    # Así arranca ya en el sitio correcto y encuentra el XML automáticamente.
    exe = Path(st.exe_path)
    cmd = [str(exe), "--directory", str(xml_path.parent)]

    log.info("Lanzando MPC Autofill: %s", " ".join(cmd))
    # En Windows, DETACHED_PROCESS + CREATE_NEW_CONSOLE para que salga la ventana propia
    kwargs: dict = {"cwd": str(exe.parent)}
    if sys.platform == "win32":
        # Constantes de CreationFlags de Windows
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_CONSOLE = 0x00000010
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_CONSOLE
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid
