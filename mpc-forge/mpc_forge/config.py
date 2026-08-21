"""Configuración global: paths de datos, defaults, tiers de precio MPC."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_data_dir


APP_NAME = "MPC-Forge"
# False evita el nivel intermedio "local\" en Windows AppData.
APP_AUTHOR: str | bool = False

# El User-Agent es requerido por la API no oficial de Moxfield. Debe ser
# identificable y contener contacto — usa el tuyo propio en producción.
MOXFIELD_USER_AGENT = "MPC-Forge/0.1 (personal-proxy-tool; contact: local)"

SCRYFALL_USER_AGENT = "MPC-Forge/0.1"
SCRYFALL_API = "https://api.scryfall.com"

# Los tiers de MakePlayingCards por tamaño de tirada (cartas por deck de 55).
# Precios son orientativos (USD, standard S30, cara+dorso). Editables en runtime.
MPC_TIERS: list[dict[str, float]] = [
    {"size": 18, "unit_usd": 0.51},
    {"size": 36, "unit_usd": 0.43},
    {"size": 55, "unit_usd": 0.34},
    {"size": 72, "unit_usd": 0.30},
    {"size": 90, "unit_usd": 0.28},
    {"size": 108, "unit_usd": 0.26},
    {"size": 126, "unit_usd": 0.25},
    {"size": 180, "unit_usd": 0.22},
    {"size": 234, "unit_usd": 0.20},
    {"size": 306, "unit_usd": 0.19},
    {"size": 396, "unit_usd": 0.18},
    {"size": 504, "unit_usd": 0.17},
    {"size": 612, "unit_usd": 0.16},
]

# Stocks disponibles en MPC-Autofill (los valores exactos que espera el XML).
CARDSTOCK_OPTIONS = [
    "(S30) Standard Smooth",
    "(S33) Superior Smooth",
    "(M31) Linen",
    "(M32) Superior Linen",
    "(P10) Plastic",
]

DEFAULT_CARDSTOCK = "(S30) Standard Smooth"
DEFAULT_CARDBACK_NAME = "default-back"

# --- Conversión USD → EUR y shipping ----------------------------------------
# Tipo de cambio orientativo. Editable en runtime. Los precios de MPC son en USD,
# pero al usuario final le mostramos EUR con envío para tener el coste total real.
USD_TO_EUR = 0.92

# Shipping MPC → EU: base internacional + extra tramo europeo (según tu experiencia).
SHIPPING_BASE_EUR = 12.99
SHIPPING_EU_EXTRA_EUR = 7.58
SHIPPING_TOTAL_EUR_EU = round(SHIPPING_BASE_EUR + SHIPPING_EU_EXTRA_EUR, 2)

# --- Integraciones opcionales ----------------------------------------------
# Ruta al ejecutable de mpc-autofill (chilli-axe/mpc-autofill desktop tool).
# Si está vacío, la app busca el binario en el PATH y en carpetas comunes.
# Editable desde la UI de Ajustes.
MPC_AUTOFILL_EXE_PATH = ""

# Google Drive API key (opcional). Si se configura, se usa para indexar los
# drives comunitarios de forma rápida y fiable. Sin ella, se cae a scraping
# HTML del embedded viewer (funciona pero es más lento e incompleto).
# Se obtiene gratis en https://console.cloud.google.com/apis/credentials
# Editable desde la UI de Ajustes.
GOOGLE_API_KEY = ""


@dataclass(frozen=True)
class Paths:
    """Rutas del filesystem que usa la app."""
    data_dir: Path
    db_path: Path
    art_dir: Path
    custom_art_dir: Path
    exports_dir: Path
    backups_dir: Path
    cardbacks_dir: Path

    @classmethod
    def default(cls) -> "Paths":
        root = Path(user_data_dir(APP_NAME, APP_AUTHOR))
        art = root / "art"
        custom_art = root / "custom_art"
        exports = root / "exports"
        backups = root / "backups"
        cardbacks = root / "cardbacks"
        for d in (root, art, custom_art, exports, backups, cardbacks):
            d.mkdir(parents=True, exist_ok=True)
        return cls(
            data_dir=root,
            db_path=root / "mpc_forge.sqlite3",
            art_dir=art,
            custom_art_dir=custom_art,
            exports_dir=exports,
            backups_dir=backups,
            cardbacks_dir=cardbacks,
        )


@dataclass
class Settings:
    """Ajustes editables por el usuario."""
    default_cardstock: str = DEFAULT_CARDSTOCK
    default_cardback: str = DEFAULT_CARDBACK_NAME
    foil_default: bool = False
    prefer_full_art: bool = False
    prefer_borderless: bool = False
    mpc_tiers: list[dict[str, float]] = field(default_factory=lambda: list(MPC_TIERS))


PATHS = Paths.default()
