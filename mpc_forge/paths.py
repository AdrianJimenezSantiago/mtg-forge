from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass is not None:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def template_dir() -> Path:
    return resource_root() / "templates"


def static_dir() -> Path:
    return resource_root() / "static"


def diagnose() -> str:
    lines = [
        f"paths.is_frozen     = {is_frozen()}",
        f"paths.install_root  = {install_root()}",
        f"paths.resource_root = {resource_root()}",
        f"paths.template_dir  = {template_dir()}",
        f"paths.static_dir    = {static_dir()}",
        f"sys.executable      = {sys.executable}",
        f"sys._MEIPASS        = {getattr(sys, '_MEIPASS', '(not set)')}",
    ]
    return "\n".join(lines)
