"""Ajustes runtime persistentes en la tabla KeyValue.

Todo lo que aquí se defina se guarda en la BD y sobrescribe los defaults de
`mpc_forge.config` al arrancar. El usuario los edita desde la UI de Ajustes.

Cada setting tiene:
- clave estable
- tipo (str/float/bool/int/json)
- valor por defecto (viene de config.py)
- descripción y grupo (para la UI)

`get_all()` devuelve el snapshot completo. `set_many()` guarda cambios.
`apply_to_config()` propaga los valores a los módulos que los usan (mediante
mutación de las variables globales en `mpc_forge.config`).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import KeyValue

log = logging.getLogger(__name__)

SettingType = Literal["str", "float", "int", "bool", "json"]


@dataclass
class SettingDef:
    key: str
    label: str
    type: SettingType
    group: str
    default: Any
    description: str = ""
    min_value: float | None = None
    max_value: float | None = None
    choices: list[str] | None = None


# Registry de settings expuestos en la UI.
# Los defaults se toman de config.py — si se cambia allí, se propaga al primer arranque.
DEFINITIONS: list[SettingDef] = [
    # --- Precio & envío ---
    SettingDef(
        key="usd_to_eur",
        label="Tipo de cambio USD → EUR",
        type="float",
        group="Precio y envío",
        default=cfg.USD_TO_EUR,
        description="Se aplica al convertir los precios de MPC (USD) a euros.",
        min_value=0.1, max_value=10.0,
    ),
    SettingDef(
        key="shipping_base_eur",
        label="Envío base (EUR)",
        type="float",
        group="Precio y envío",
        default=cfg.SHIPPING_BASE_EUR,
        description="Coste fijo de envío internacional MPC.",
        min_value=0.0, max_value=100.0,
    ),
    SettingDef(
        key="shipping_eu_extra_eur",
        label="Envío extra EU (EUR)",
        type="float",
        group="Precio y envío",
        default=cfg.SHIPPING_EU_EXTRA_EUR,
        description="Extra para envíos dentro de la Unión Europea.",
        min_value=0.0, max_value=100.0,
    ),

    # --- Defaults del XML/PDF ---
    SettingDef(
        key="default_cardstock",
        label="Stock por defecto",
        type="str",
        group="Impresión (defaults)",
        default=cfg.DEFAULT_CARDSTOCK,
        description="Se preselecciona en el editor de mazo antes de generar el XML.",
        choices=list(cfg.CARDSTOCK_OPTIONS),
    ),
    SettingDef(
        key="foil_default",
        label="Foil por defecto",
        type="bool",
        group="Impresión (defaults)",
        default=False,
        description="Marca la casilla de foil al abrir un mazo.",
    ),
    SettingDef(
        key="default_cardback_name",
        label="Nombre del cardback por defecto",
        type="str",
        group="Impresión (defaults)",
        default=cfg.DEFAULT_CARDBACK_NAME,
        description="Se busca en la carpeta de cardbacks como <nombre>.png/.jpg.",
    ),

    # --- Preferencias de arte ---
    SettingDef(
        key="prefer_full_art",
        label="Preferir full art",
        type="bool",
        group="Preferencias de arte",
        default=False,
        description="Al abrir la galería, activa el filtro «Full art» automáticamente.",
    ),
    SettingDef(
        key="prefer_borderless",
        label="Preferir borderless",
        type="bool",
        group="Preferencias de arte",
        default=False,
        description="Al abrir la galería, activa el filtro «Sin borde» automáticamente.",
    ),

    # --- HTTP / integraciones ---
    SettingDef(
        key="moxfield_user_agent",
        label="User-Agent para Moxfield",
        type="str",
        group="Integraciones",
        default=cfg.MOXFIELD_USER_AGENT,
        description="Debería ser identificable con tu contacto. Cortesía con Moxfield.",
    ),
    SettingDef(
        key="mpc_autofill_exe_path",
        label="Ejecutable de MPC Autofill",
        type="str",
        group="Integraciones",
        default="",
        description=(
            "Ruta al binario del desktop tool (chilli-axe/mpc-autofill). "
            "Si lo dejas vacío, la app lo busca en el PATH y en la carpeta del proyecto. "
            "Descárgalo de github.com/chilli-axe/mpc-autofill/releases."
        ),
    ),
    SettingDef(
        key="google_api_key",
        label="Google API key (Drive)",
        type="str",
        group="Integraciones",
        default="",
        description=(
            "Opcional pero recomendado. Se usa para indexar los Google Drives comunitarios "
            "y hacer búsqueda fuzzy de artes. Gratis en console.cloud.google.com "
            "(APIs & Services → Credentials → API key, y habilita 'Google Drive API'). "
            "Cuota: 10.000 requests/día."
        ),
    ),
]

_DEFS_BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFINITIONS}


def _coerce(sd: SettingDef, raw: str) -> Any:
    if sd.type == "float":
        return float(raw)
    if sd.type == "int":
        return int(raw)
    if sd.type == "bool":
        return raw.lower() in {"1", "true", "yes", "on"}
    if sd.type == "json":
        return json.loads(raw)
    return raw


def _serialize(sd: SettingDef, value: Any) -> str:
    if sd.type == "bool":
        return "true" if value else "false"
    if sd.type == "json":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


async def get_all(db: AsyncSession) -> dict[str, Any]:
    """Snapshot actual de todos los settings, con defaults aplicados si faltan."""
    rows = (await db.scalars(select(KeyValue).where(KeyValue.key.like("settings.%")))).all()
    stored: dict[str, str] = {r.key[len("settings."):]: r.value for r in rows}
    out: dict[str, Any] = {}
    for sd in DEFINITIONS:
        raw = stored.get(sd.key)
        if raw is None:
            out[sd.key] = sd.default
        else:
            try:
                out[sd.key] = _coerce(sd, raw)
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("Setting %s corrupto (%s), usando default", sd.key, e)
                out[sd.key] = sd.default
    return out


async def set_many(db: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
    """Guarda los valores indicados y devuelve el snapshot actualizado."""
    for key, value in updates.items():
        sd = _DEFS_BY_KEY.get(key)
        if not sd:
            log.warning("Setting desconocido ignorado: %s", key)
            continue
        if sd.choices and str(value) not in sd.choices:
            raise ValueError(f"Valor no válido para {key}: {value!r}. Opciones: {sd.choices}")
        if sd.type in {"float", "int"}:
            fv = float(value)
            if sd.min_value is not None and fv < sd.min_value:
                raise ValueError(f"{key} debe ser >= {sd.min_value}")
            if sd.max_value is not None and fv > sd.max_value:
                raise ValueError(f"{key} debe ser <= {sd.max_value}")
        serialized = _serialize(sd, value)
        existing = await db.get(KeyValue, f"settings.{sd.key}")
        if existing:
            existing.value = serialized
        else:
            db.add(KeyValue(key=f"settings.{sd.key}", value=serialized))
    await db.commit()
    snapshot = await get_all(db)
    apply_to_config(snapshot)
    return snapshot


def apply_to_config(values: dict[str, Any]) -> None:
    """Propaga los settings a las variables globales de mpc_forge.config.

    Con esto, cualquier módulo que lea `cfg.USD_TO_EUR` verá el valor actual sin
    tener que reiniciar la app.
    """
    for key, value in values.items():
        if key == "usd_to_eur":
            cfg.USD_TO_EUR = float(value)
        elif key == "shipping_base_eur":
            cfg.SHIPPING_BASE_EUR = float(value)
        elif key == "shipping_eu_extra_eur":
            cfg.SHIPPING_EU_EXTRA_EUR = float(value)
            cfg.SHIPPING_TOTAL_EUR_EU = round(cfg.SHIPPING_BASE_EUR + cfg.SHIPPING_EU_EXTRA_EUR, 2)
        elif key == "default_cardstock":
            cfg.DEFAULT_CARDSTOCK = str(value)
        elif key == "default_cardback_name":
            cfg.DEFAULT_CARDBACK_NAME = str(value)
        elif key == "moxfield_user_agent":
            cfg.MOXFIELD_USER_AGENT = str(value)
        elif key == "mpc_autofill_exe_path":
            cfg.MPC_AUTOFILL_EXE_PATH = str(value)
        elif key == "google_api_key":
            cfg.GOOGLE_API_KEY = str(value).strip()
        # foil_default y prefer_* los consume solo el frontend.


def definitions_dump() -> list[dict[str, Any]]:
    """Serialización para la UI: cada setting con su meta + default."""
    out = []
    for sd in DEFINITIONS:
        out.append({
            "key": sd.key,
            "label": sd.label,
            "type": sd.type,
            "group": sd.group,
            "default": sd.default,
            "description": sd.description,
            "min_value": sd.min_value,
            "max_value": sd.max_value,
            "choices": sd.choices,
        })
    return out
