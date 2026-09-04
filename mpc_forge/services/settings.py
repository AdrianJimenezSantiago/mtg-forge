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


# Snapshot cacheado en memoria del último ``get_all``. La app es single-process
# y todos los writes pasan por ``set_many``, así que invalidar ahí basta para
# mantener la coherencia. Ver ``get_all`` / ``set_many``.
_cached_snapshot: dict[str, Any] | None = None

SettingType = Literal["str", "float", "int", "bool", "json", "path"]


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
    # --- General (impresión + preferencias de arte + idioma) ---
    SettingDef(
        key="default_cardstock",
        label="Stock por defecto",
        type="str",
        group="General",
        default=cfg.DEFAULT_CARDSTOCK,
        description="Se preselecciona en el editor de mazo antes de generar el XML.",
        choices=list(cfg.CARDSTOCK_OPTIONS),
    ),
    SettingDef(
        key="foil_default",
        label="Foil por defecto",
        type="bool",
        group="General",
        default=False,
        description="Marca la casilla de foil al abrir un mazo.",
    ),
    SettingDef(
        key="default_cardback_name",
        label="Nombre del cardback por defecto",
        type="str",
        group="General",
        default=cfg.DEFAULT_CARDBACK_NAME,
        description="Se busca en la carpeta de cardbacks como <nombre>.png/.jpg.",
    ),
    SettingDef(
        key="preferred_language",
        label="Idioma preferido de las cartas",
        type="str",
        group="General",
        default="en",
        description=(
            "Al pulsar «Idioma ▾» en el editor de un mazo, este idioma vendrá preseleccionado. "
            "Scryfall solo tiene arte en el idioma pedido si esa impresión existe: promo, "
            "secret lair y otras solo hay en inglés y se conservarán tal cual."
        ),
        choices=["en", "es", "fr", "de", "it", "pt", "ja", "ko", "ru", "zhs", "zht"],
    ),
    SettingDef(
        key="prefer_full_art",
        label="Preferir full art",
        type="bool",
        group="General",
        default=False,
        description="Al abrir la galería, activa el filtro «Full art» automáticamente.",
    ),
    SettingDef(
        key="prefer_borderless",
        label="Preferir borderless",
        type="bool",
        group="General",
        default=False,
        description="Al abrir la galería, activa el filtro «Sin borde» automáticamente.",
    ),

    # --- Precios y envío ---
    SettingDef(
        key="usd_to_eur",
        label="Tipo de cambio USD → EUR",
        type="float",
        group="Precios y envío",
        default=cfg.USD_TO_EUR,
        description="Se aplica al convertir los precios de MPC (USD) a euros.",
        min_value=0.1, max_value=10.0,
    ),
    SettingDef(
        key="shipping_base_eur",
        label="Envío base (EUR)",
        type="float",
        group="Precios y envío",
        default=cfg.SHIPPING_BASE_EUR,
        description="Coste fijo de envío internacional MPC.",
        min_value=0.0, max_value=100.0,
    ),
    SettingDef(
        key="shipping_eu_extra_eur",
        label="Envío extra EU (EUR)",
        type="float",
        group="Precios y envío",
        default=cfg.SHIPPING_EU_EXTRA_EUR,
        description="Extra para envíos dentro de la Unión Europea.",
        min_value=0.0, max_value=100.0,
    ),

    # --- Red y conexión ---
    SettingDef(
        key="moxfield_user_agent",
        label="User-Agent para Moxfield",
        type="str",
        group="Red y conexión",
        default=cfg.MOXFIELD_USER_AGENT,
        description="Debería ser identificable con tu contacto. Cortesía con Moxfield.",
    ),
    SettingDef(
        key="google_api_key",
        label="Google API key (Drive)",
        type="str",
        group="Red y conexión",
        default="",
        description=(
            "Opcional pero recomendado. Se usa para indexar los Google Drives comunitarios "
            "y hacer búsqueda fuzzy de artes. Gratis en console.cloud.google.com "
            "(APIs & Services → Credentials → API key, y habilita 'Google Drive API'). "
            "Cuota: 10.000 requests/día."
        ),
    ),
    SettingDef(
        key="ssl_insecure",
        label="Desactivar verificación SSL",
        type="bool",
        group="Red y conexión",
        default=False,
        description=(
            "Solo si tu red corporativa intercepta HTTPS con una CA que ni truststore "
            "reconoce y ves errores de CERTIFICATE_VERIFY_FAILED. Requiere reiniciar la app. "
            "Equivale a la variable de entorno MPC_FORGE_INSECURE_SSL=1."
        ),
    ),

    # --- MPC Autofill ---
    SettingDef(
        key="mpc_autofill_exe_path",
        label="Ejecutable de MPC Autofill",
        type="str",
        group="MPC Autofill",
        default="",
        description=(
            "Ruta al binario del desktop tool (chilli-axe/mpc-autofill). "
            "Si lo dejas vacío, la app lo busca en el PATH y en la carpeta del proyecto. "
            "Descárgalo de github.com/chilli-axe/mpc-autofill/releases."
        ),
    ),

    # --- Ubicación de datos ---
    # Todos son opcionales. Si están vacíos (default), se usa la ruta bajo
    # ``<install>/user-settings/<nombre>/``. Solo directorios de contenido —
    # la BD siempre queda en ``<install>/user-settings/`` para evitar mover
    # una BD con handles abiertos.
    SettingDef(
        key="paths.art_dir",
        label="Cache de artes (Scryfall)",
        type="path",
        group="Ubicación de datos",
        default="",
        description=(
            "Miniaturas y arte descargados de Scryfall. Puede crecer varios GB con "
            "uso intensivo — mover a un disco distinto si va justo de espacio. "
            "Vacío = usa el default junto al ejecutable."
        ),
    ),
    SettingDef(
        key="paths.custom_art_dir",
        label="Arte custom del usuario",
        type="path",
        group="Ubicación de datos",
        default="",
        description=(
            "Imágenes locales que sustituyen al arte oficial de cada carta. "
            "Vacío = usa el default junto al ejecutable."
        ),
    ),
    SettingDef(
        key="paths.exports_dir",
        label="XMLs y PDFs generados",
        type="path",
        group="Ubicación de datos",
        default="",
        description=(
            "Aquí se guardan los ficheros de salida (XML para MPC Autofill, PDFs de "
            "proxies). Útil apuntar a una carpeta compartida si trabajas en varios PCs. "
            "Vacío = usa el default junto al ejecutable."
        ),
    ),
    SettingDef(
        key="paths.backups_dir",
        label="Backups (.zip)",
        type="path",
        group="Ubicación de datos",
        default="",
        description=(
            "Los backups manuales se comprimen aquí. Recomendable apuntar a un disco "
            "distinto o carpeta sincronizada con la nube (Dropbox, OneDrive). "
            "Vacío = usa el default junto al ejecutable."
        ),
    ),
    SettingDef(
        key="paths.cardbacks_dir",
        label="Cardbacks (reversos)",
        type="path",
        group="Ubicación de datos",
        default="",
        description=(
            "Imágenes de reverso disponibles para el picker. Se referencian por nombre "
            "de fichero desde el editor. Vacío = usa el default junto al ejecutable."
        ),
    ),
    # --- Búsqueda avanzada (Fase 2 · T8) ---
    SettingDef(
        key="phash.enabled",
        label="Detectar imágenes duplicadas (pHash)",
        type="bool",
        group="Búsqueda avanzada",
        default=False,
        description=(
            "Calcula un hash perceptual de cada arte para agrupar imágenes idénticas "
            "aunque estén en drives distintos o tengan variaciones leves de compresión. "
            "Requiere descargar los thumbnails (~50KB cada uno). Activarlo puede "
            "tardar varios minutos la primera vez si tienes muchos drives indexados. "
            "Requiere Pillow e imagehash instaladas."
        ),
    ),
    SettingDef(
        key="phash.threshold",
        label="Umbral de similitud pHash",
        type="int",
        group="Búsqueda avanzada",
        default=8,
        description=(
            "Distancia de Hamming máxima para considerar dos imágenes 'iguales'. "
            "Más bajo = más estricto (solo copias casi idénticas). Más alto = agrupa "
            "también variantes con recorte / watermark. Rango típico: 4-12."
        ),
        min_value=0,
        max_value=32,
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
    if sd.type == "path":
        # Guardamos el path como string tal cual — la validación real (existencia
        # del padre, permisos de escritura) la hace Paths.with_overrides() al
        # aplicarlo. Aquí solo aseguramos que sea un string sin espacios laterales.
        return raw.strip()
    return raw


def _serialize(sd: SettingDef, value: Any) -> str:
    if sd.type == "bool":
        return "true" if value else "false"
    if sd.type == "json":
        return json.dumps(value, ensure_ascii=False)
    if sd.type == "path":
        # Normalizamos: strip + collapse de espacios. NO resolvemos absolute path
        # aquí para respetar exactamente lo que el usuario escribió (útil para
        # ver "vacío" vs "ruta explícita").
        return str(value).strip()
    return str(value)


async def get_all(db: AsyncSession) -> dict[str, Any]:
    """Snapshot actual de todos los settings, con defaults aplicados si faltan.

    El resultado se cachea en ``_cached_snapshot`` — la app es single-process
    y todos los writes pasan por :func:`set_many`, que invalida el cache. La
    segunda llamada (y siguientes) no toca la BD.
    """
    global _cached_snapshot
    if _cached_snapshot is not None:
        return _cached_snapshot

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

    _cached_snapshot = out
    return out


async def set_many(db: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
    """Guarda los valores indicados y devuelve el snapshot actualizado.

    Batch prefetch de KeyValues existentes: 1 SELECT WHERE key IN (?) en vez de
    N queries individuales. Invalida el cache de ``get_all`` antes de releerlo.
    """
    global _cached_snapshot

    valid_updates: dict[str, tuple[Any, str]] = {}
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
        valid_updates[key] = (value, _serialize(sd, value))

    if not valid_updates:
        return await get_all(db)

    kv_keys = [f"settings.{k}" for k in valid_updates]
    existing_rows = (
        await db.scalars(
            select(KeyValue).where(KeyValue.key.in_(kv_keys))
        )
    ).all()
    existing_by_key: dict[str, KeyValue] = {kv.key: kv for kv in existing_rows}

    for key, (_raw, serialized) in valid_updates.items():
        kv_key = f"settings.{key}"
        existing = existing_by_key.get(kv_key)
        if existing:
            existing.value = serialized
        else:
            db.add(KeyValue(key=kv_key, value=serialized))
    await db.commit()

    _cached_snapshot = None
    snapshot = await get_all(db)
    apply_to_config(snapshot)
    return snapshot


def apply_to_config(values: dict[str, Any]) -> None:
    """Propaga los settings a las variables globales de mpc_forge.config.

    Con esto, cualquier módulo que lea `cfg.USD_TO_EUR` verá el valor actual sin
    tener que reiniciar la app. Para los ``paths.*`` recomponemos ``cfg.PATHS``
    aplicando los overrides sobre el default — los servicios que leen
    ``cfg.PATHS.art_dir`` etc dinámicamente ven la nueva ruta en la siguiente
    llamada.
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
        elif key == "ssl_insecure":
            # Propaga al módulo ssl_config, que combina este flag con la env var.
            # Cambiar en runtime marca el flag pero NO reconfigura los clientes
            # HTTPX ya instanciados — la UI advierte que hace falta reiniciar.
            from mpc_forge import ssl_config as _ssl
            _ssl.set_runtime_insecure(bool(value))
        # foil_default, prefer_*, preferred_language los consume solo el frontend.

    # --- Paths personalizables ---
    # Se procesan aparte porque cambiar cualquiera implica recomponer cfg.PATHS
    # entero con with_overrides(). Solo lo hacemos si hay al menos un path.* en
    # los updates (evita rebuild innecesario cuando el usuario solo tocó, por
    # ejemplo, el tipo de cambio USD→EUR).
    path_keys = {"paths.art_dir", "paths.custom_art_dir", "paths.exports_dir",
                 "paths.backups_dir", "paths.cardbacks_dir"}
    if path_keys & values.keys():
        # Partimos SIEMPRE de los defaults (no del cfg.PATHS actual). Así, si el
        # usuario acaba de vaciar un override, restauramos su default correctamente.
        base = cfg.Paths.default()
        cfg.PATHS = base.with_overrides(
            art_dir=values.get("paths.art_dir") or None,
            custom_art_dir=values.get("paths.custom_art_dir") or None,
            exports_dir=values.get("paths.exports_dir") or None,
            backups_dir=values.get("paths.backups_dir") or None,
            cardbacks_dir=values.get("paths.cardbacks_dir") or None,
        )


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
