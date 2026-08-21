# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — modo --onedir (carpeta con MPC-Forge.exe dentro).
# Build:  pyinstaller packaging/mpc-forge.spec --noconfirm
# Output: packaging/dist/MPC-Forge/  → esta carpeta es lo que se distribuye.

from pathlib import Path

# La spec se ejecuta desde la raíz del proyecto (donde está requirements.txt)
PROJECT_ROOT = Path.cwd()

# Recursos que necesita la app en runtime y no están dentro de mpc_forge/
DATAS = [
    (str(PROJECT_ROOT / "templates"),  "templates"),
    (str(PROJECT_ROOT / "static"),     "static"),
]

# Módulos que PyInstaller no siempre descubre solo (algunos son cargados
# dinámicamente por FastAPI, uvicorn, sqlalchemy plugins, etc.)
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "aiosqlite",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "email.mime.multipart",   # requerido por algunas deps indirectas
    "email.mime.text",
]


a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Cosas pesadas que no usamos:
        "tkinter",
        "matplotlib",
        "PIL.ImageTk",
        "notebook",
        "IPython",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MPC-Forge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX puede disparar antivirus, mejor sin
    console=True,        # ventana de consola visible para logs y Ctrl+C
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,           # sin icono personalizado (usa el default)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MPC-Forge",
)
