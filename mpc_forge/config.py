"""Configuración global: paths de datos, defaults, tiers de precio MPC."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "MPC-Forge"
APP_AUTHOR: str | bool = False

MOXFIELD_USER_AGENT = "MPC-Forge/0.1 (personal-proxy-tool; contact: local)"

SCRYFALL_USER_AGENT = "MPC-Forge/0.1"
SCRYFALL_API = "https://api.scryfall.com"

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

CARDSTOCK_OPTIONS = [
    "(S30) Standard Smooth",
    "(S33) Superior Smooth",
    "(M31) Linen",
    "(M32) Superior Linen",
    "(P10) Plastic",
]

DEFAULT_CARDSTOCK = "(S30) Standard Smooth"
DEFAULT_CARDBACK_NAME = "default-back"

USD_TO_EUR = 0.92

SHIPPING_BASE_EUR = 12.99
SHIPPING_EU_EXTRA_EUR = 7.58
SHIPPING_TOTAL_EUR_EU = round(SHIPPING_BASE_EUR + SHIPPING_EU_EXTRA_EUR, 2)

MPC_AUTOFILL_EXE_PATH = ""

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
    thumbs_dir: Path

    @classmethod
    def default(cls) -> Paths:
        """Rutas por defecto: modo portable (junto al .exe) con fallback.

        Prioridad:
          1. ``<carpeta del .exe|proyecto>/user-settings/`` — modo portable.
             Se usa si podemos escribir en la carpeta de instalación.
          2. ``platformdirs.user_data_dir("MPC-Forge")`` — fallback típico
             cuando el .exe está en ``Program Files\\`` (read-only para
             usuarios normales) o en un pendrive protegido contra escritura.

        La comprobación se hace intentando crear el directorio y escribir un
        archivo minúsculo. Si falla, cae al AppData del usuario.
        """
        from mpc_forge.paths import install_root

        portable = install_root() / "user-settings"
        root: Path
        try:
            portable.mkdir(parents=True, exist_ok=True)
            probe = portable / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            root = portable
        except OSError:
            root = Path(user_data_dir(APP_NAME, APP_AUTHOR))

        art = root / "art"
        custom_art = root / "custom_art"
        exports = root / "exports"
        backups = root / "backups"
        cardbacks = root / "cardbacks"
        thumbs = root / "thumbs"
        for d in (root, art, custom_art, exports, backups, cardbacks, thumbs):
            d.mkdir(parents=True, exist_ok=True)
        return cls(
            data_dir=root,
            db_path=root / "mpc_forge.sqlite3",
            art_dir=art,
            custom_art_dir=custom_art,
            exports_dir=exports,
            backups_dir=backups,
            cardbacks_dir=cardbacks,
            thumbs_dir=thumbs,
        )

    def with_overrides(
        self,
        art_dir: str | Path | None = None,
        custom_art_dir: str | Path | None = None,
        exports_dir: str | Path | None = None,
        backups_dir: str | Path | None = None,
        cardbacks_dir: str | Path | None = None,
        thumbs_dir: str | Path | None = None,
    ) -> Paths:
        """Devuelve una nueva Paths con los overrides aplicados.

        Los overrides son las rutas que el usuario ha personalizado desde la UI
        de Ajustes. Solo los directorios de contenido son personalizables — la
        BD y ``data_dir`` NUNCA se cambian aquí (mover una BD abierta es
        peligroso, y hay handles de logging apuntando ahí).

        Un override vacío/None mantiene el path por defecto. Si un override es
        un path válido, se crea el directorio si no existe.
        """
        def _pick(override, default):
            if override is None or str(override).strip() == "":
                return default
            try:
                p = Path(str(override)).expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError):
                return default
            return p

        return Paths(
            data_dir=self.data_dir,
            db_path=self.db_path,
            art_dir=_pick(art_dir, self.art_dir),
            custom_art_dir=_pick(custom_art_dir, self.custom_art_dir),
            exports_dir=_pick(exports_dir, self.exports_dir),
            backups_dir=_pick(backups_dir, self.backups_dir),
            cardbacks_dir=_pick(cardbacks_dir, self.cardbacks_dir),
            thumbs_dir=_pick(thumbs_dir, self.thumbs_dir),
        )


PATHS = Paths.default()
