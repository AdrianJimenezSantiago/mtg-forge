"""Resolución de rutas a recursos (templates, static, skills).

Funciona en dos modos:
- Desarrollo: recursos junto al código, en la raíz del proyecto.
- Empaquetado (PyInstaller): recursos junto al .exe (modo --onedir) o dentro
  del bundle temporal (modo --onefile). Detectamos con `sys.frozen`.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True si la app corre desde un .exe empaquetado con PyInstaller."""
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """Raíz donde viven los recursos (templates, static).

    Dev  → carpeta del proyecto (mpc_forge/../).
    Frozen --onedir → carpeta del .exe (recursos van junto).
    Frozen --onefile → sys._MEIPASS (carpeta temporal de extracción).
    """
    if is_frozen():
        # PyInstaller onefile pone recursos en _MEIPASS
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            return Path(meipass)
        # PyInstaller onedir: junto al .exe
        return Path(sys.executable).resolve().parent
    # Desarrollo: subimos dos niveles desde mpc_forge/paths.py
    return Path(__file__).resolve().parent.parent


def template_dir() -> Path:
    return resource_root() / "templates"


def static_dir() -> Path:
    return resource_root() / "static"
