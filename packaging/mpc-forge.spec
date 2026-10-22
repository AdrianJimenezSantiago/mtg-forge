
import sys
from pathlib import Path

PROJECT_ROOT = Path.cwd()

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

_VENDOR = PROJECT_ROOT / "static" / "vendor"
_REQUIRED_ASSETS = [
    "tailwind.css", "alpine.min.js", "alpine-collapse.min.js",
    "alpine-focus.min.js", "auto-animate.min.js", "lucide.min.js",
    "mana.min.css", "fonts/mana.woff2",
]
_missing = [a for a in _REQUIRED_ASSETS if not (_VENDOR / a).exists()]
if _missing:
    raise SystemExit(
        "\nFaltan assets vendorizados: " + ", ".join(_missing) +
        "\nEjecuta antes de compilar:\n"
        "    npm install && npm run vendor && npm run build:css\n"
    )

# El perfil de color con el que se separan las exportaciones para imprenta.
_ICC = PROJECT_ROOT / "resources" / "icc" / "Coated_Fogra39L_VIGC_300.icc"
if not _ICC.is_file():
    raise SystemExit(f"\nFalta el perfil ICC de imprenta: {_ICC}\n")

DATAS = [
    (str(PROJECT_ROOT / "templates"),  "templates"),
    (str(PROJECT_ROOT / "static"),     "static"),
    (str(PROJECT_ROOT / "resources"),  "resources"),
]

_LOCALES = PROJECT_ROOT / "locales"
if _LOCALES.is_dir():
    DATAS.append((str(_LOCALES), "locales"))

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
    "email.mime.multipart",
    "email.mime.text",
    # python-slugify 9 lo importa con importlib al transliterar, así que el
    # análisis de PyInstaller no lo ve; sin él cualquier exportación da 500.
    "text_unidecode",
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
        "tkinter",
        "matplotlib",
        "PIL.ImageTk",
        "notebook",
        "IPython",
        "pytest",
        "pywt",
        "scipy.integrate",
        "scipy.interpolate",
        "scipy.io",
        "scipy.ndimage",
        "scipy.optimize",
        "scipy.signal",
        "scipy.sparse",
        "scipy.spatial",
        "scipy.stats",
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
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "packaging" / "icon.ico") if IS_WINDOWS else None,
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

if IS_MACOS:
    app = BUNDLE(  # noqa: F821
        coll,
        name="MPC Forge.app",
        icon=None,
        bundle_identifier="com.adrianjimenezsantiago.mpcforge",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        },
    )
