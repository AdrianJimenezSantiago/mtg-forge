# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — modo --onedir (carpeta con MPC-Forge.exe dentro).
# Build:  pyinstaller packaging/mpc-forge.spec --noconfirm
# Output: packaging/dist/MPC-Forge/  → esta carpeta es lo que se distribuye.

from pathlib import Path

# La spec se ejecuta desde la raíz del proyecto (donde está requirements.txt)
PROJECT_ROOT = Path.cwd()

# --- Guardia: los assets vendorizados tienen que existir --------------------
# La interfaz ya no carga nada desde CDN: Tailwind, Alpine, Lucide, Chart.js y
# mana-font se sirven desde static/vendor/. Si esa carpeta falta, el .exe se
# compila igualmente pero la app arranca SIN ESTILOS, y el fallo solo se
# descubre al ejecutar el binario. Mejor romper el build aquí.
_VENDOR = PROJECT_ROOT / "static" / "vendor"
_REQUIRED_ASSETS = [
    "tailwind.css", "alpine.min.js", "alpine-collapse.min.js",
    "alpine-focus.min.js", "lucide.min.js", "chart.umd.js",
    "mana.min.css", "fonts/mana.woff2",
]
_missing = [a for a in _REQUIRED_ASSETS if not (_VENDOR / a).exists()]
if _missing:
    raise SystemExit(
        "\nFaltan assets vendorizados: " + ", ".join(_missing) +
        "\nEjecuta antes de compilar:\n"
        "    npm install && npm run vendor && npm run build:css\n"
    )

# Recursos que necesita la app en runtime y no están dentro de mpc_forge/
# `static` entra entero, incluido `static/vendor` (assets de terceros) y
# `static/js` (los módulos ES del frontend).
DATAS = [
    (str(PROJECT_ROOT / "templates"),  "templates"),
    (str(PROJECT_ROOT / "static"),     "static"),
    # Los locales de i18n son ficheros JSON externos desde el refactor de
    # traducciones; sin esto, el .exe arrancaría solo con las claves crudas.
    (str(PROJECT_ROOT / "locales"),    "locales"),
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
    icon=str(PROJECT_ROOT / "packaging" / "icon.ico"),   # icono del .exe (multi-resolución)
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
