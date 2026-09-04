"""Gestión de sources de arte custom (Google Drives comunitarios y otros).

Filosofía: no dependemos de ningún backend externo. El usuario gestiona su
propia lista de drives desde la UI, con nombre + URL + descripción + tags.

Cada source es un enlace que el usuario abre en el navegador. Descarga las
imágenes que quiera y las añade a MPC Forge de una de estas dos formas:
  1. Copiándolas a %APPDATA%/MPC-Forge/custom_art/ con el nombre de la carta
  2. Con el botón "+ Añadir por URL" del editor de mazos (pegando la URL directa
     de Google Drive: https://drive.google.com/uc?id=<FILE_ID>&export=download)

Al arrancar por primera vez, sembramos algunos drives conocidos para que el
usuario pueda empezar sin trabajo previo.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import ArtSource

log = logging.getLogger(__name__)


# Catálogo curado de drives comunitarios que MPCFill.com usa como sources.
# Nombramos "MPCFill #NN" porque no tenemos forma de saber el nombre real de
# cada uno sin visitarlo — el usuario los puede renombrar desde la UI.
# El orden es el que dio el usuario. Se pueden restaurar en cualquier momento
# desde la UI (Ajustes → botón "Restaurar catálogo").
_CURATED_CATALOG: list[tuple[str, str]] = [
    # (name, drive_folder_id)
    ("MPCFill #01", "1wI6DgeKQ1YrFIGfhsfYe4w8XzfMjvoCM"),
    ("MPCFill #02", "1jzajJLgBeVwpZJaZYPU2gY1w5U4YXl4x"),
    ("MPCFill #03", "1iIKdugU8N5jNwR1kP8MKNTmUgPVGFCx-"),
    ("MPCFill #04", "1Mtm50Xdg5Ruku2Rwg2lKHI0iDeCqAXUz"),
    ("MPCFill #05", "1rSUdFSzcofXKnUni28t_UGzumCWOm4Yu"),
    ("MPCFill #06", "1-15yknLbXb6wGjbQrJpOj5I2DpqdPpDR"),
    ("MPCFill #07", "1ZYBXsToGLRQhTu6L6QeSD6TLl1fuq7-w"),
    ("MPCFill #08", "1XbVfpU65CGtBGJ5caLVtlZzPn8Y7fQsL"),
    ("MPCFill #09", "1uRxkOHZmbtW8oAv3pWU5HpJQjMNVBrVG"),
    ("MPCFill #10", "1DtRLSze0viO5gWxYgcE1BSRU9ps84To8"),
    ("MPCFill #11", "19CpCTba4o6PXEmZEyYL00do3LtbsWGne"),
    ("MPCFill #12", "1v-AM8tDsKcXK4aLp4f0AztC4vhY5qs30"),
    ("MPCFill #13", "1J8T0TuxHWjgkSZejIWPRfRScvVYDzLk6"),
    ("MPCFill #14", "18jxr1II9b-_xWF6QU99-2ZGWjMlJrrdX"),
    ("MPCFill #15", "1I9t7PLwAVcZcYbgbduBRbn8dItvQt5mN"),
    ("MPCFill #16", "10MnHfE5aX6P-zjjyNcVDV6Mt2Z6ho4zS"),
    ("MPCFill #17", "1yBZgqZFj2SbDI9QO3zZW29fKnQSWB45I"),
    ("MPCFill #18", "12GbS8qY_mHAgtZGWeej7IWHbL819MXDn"),
    ("MPCFill #19", "1Bqa_LK5xoe1MNDd7n3-wSTLwAacYuCEn"),
    ("MPCFill #20", "1-zKMu1EvOMWiu9o3BmiEie7kS-2X1Bjn"),
    ("MPCFill #21", "1aNyomVitVpjA6y0WjhEscyunljcXdX7e"),
    ("MPCFill #22", "15VKBvSkBaVRe_X8_arhh2fDoIh6jZGL2"),
    ("MPCFill #23", "1dEnuJ48PRAt6U576iy1GqR4VeqMPNFdA"),
    ("MPCFill #24", "1-lOUivMhmWv2DLq-P5EV2dUYCav73EZh"),
    ("MPCFill #25", "1Zq1KJcSJgGs3IiFUsf0uB_2oxiyhTewQ"),
    ("MPCFill #26", "17whjecbdN1Z463FuEH5Lb7V28qph5pat"),
    ("MPCFill #27", "1Wy_civ3TCeJWiC_0ND_dlPy9YLA8_Xgk"),
    ("MPCFill #28", "1Z0saK2t86Jjy46uJ3KiTYuX4bV22bJsq"),
    ("MPCFill #29", "1-BtMshjLxsiGcHfdsWo3x4A90d0WHMts"),
    ("MPCFill #30", "1QrUcGoHSTgjgJxfCGyFImph6OtbRydWP"),
    ("MPCFill #31", "1E4zO82ZAYy0_BeOLzxNVUVBMOl1xvjli"),
    ("MPCFill #32", "12OIuGvOoqh6bIKuDSEv_Zhp1Pxs99i4M"),
    ("MPCFill #33", "1yGSkYcizX0ci_W_4ElAS0y4eO5LBo7FS"),
    ("MPCFill #34", "12mZXihSK4fNsSIQzTRIi_FPFa_VbWnTX"),
    ("MPCFill #35", "1OsCY_0roBGyV5NesMRM3TKs0x5lQoA_t"),
    ("MPCFill #36", "1GUaCqv5so59Txg3jyL2n-dpJVQxKA99l"),
    ("MPCFill #37", "1LCZup-f5JPfbuIZU-czTgbRcaoLnJrGc"),
    ("MPCFill #38", "1i-VF3HfkmnYT8la5hacHYxOhHcs-DghY"),
    ("MPCFill #39", "1_B9ZU8yPtxT9KJfovmUENRhibkg5p0yr"),
    ("MPCFill #40", "1GTQo376qfY8zanQSzQac_QYvw9Mh5k_G"),
    ("MPCFill #41", "1NYb2R-hAvoWfHqQ1t1ChxvyOtrjkzd4c"),
    ("MPCFill #42", "1c31cyBFWGGpY9zHFfetj8f0iYXAigB0x"),
    ("MPCFill #43", "1k2QWYye2h4FFry0SEiuvZMtLXOxwGR7R"),
    ("MPCFill #44", "1GgRVI2VC50iOSk-Kw5TZcUy3acISuqqr"),
    ("MPCFill #45", "1dv2QA-_s1FXrNVMMjCfdTg0pyz-b_bcC"),
    ("MPCFill #46", "1PAzD86FffjVRbqoqZl3tZtYBFBdsGVM9"),
    ("MPCFill #47", "1BC3cXfo74VHNTxGblwOB-aH-XdmJsPKs"),
    ("MPCFill #48", "1GIAirVgyvTp7KmQes3QEXpCOR8lrTjPp"),
    ("MPCFill #49", "1_iyvRowsDhpiTQZIYbvHs5suQSHbXIVG"),
    ("MPCFill #50", "11P7Rb7mEp_4zGHHS0Gt_YdsZOeQEZe-S"),
    ("MPCFill #51", "1c1zDubiWwxKa7sRttU1d1MzO4N_T91Ou"),
    ("MPCFill #52", "1Ug7h6xX1wcrk32uKsV98j8UfVKKYsx1s"),
    ("MPCFill #53", "1-78RtzNSz3YbmRb9rP6R2RTLiq7OToXk"),
    ("MPCFill #54", "1oLaPIAcMjVSKofxEBr6VPdC2MQmSGsiv"),
    ("MPCFill #55", "1-YbGAV10bIL6Inr-rIJGnnN5DfYP4BS-"),
    ("MPCFill #56", "1yc0xZ2YzzjqkmEXkeXcXG5QYgoLZRUKL"),
    ("MPCFill #57", "1Qilid0YJq8gaQlYIB5-uc8uVBI4RSBLS"),
    ("MPCFill #58", "1SBZ8epEjFbUUlBzq_IT_IARCZZRn1pHM"),
    ("MPCFill #59", "1u1b1ePLw13dnmL601hcuQQfYd6sg82z-"),
    ("MPCFill #60", "1CthJzqnnKgusM_KDsQIUukmiEfotray3"),
    ("MPCFill #61", "19usIt2WupVoZteHbLaoDtShETK98rhGd"),
    ("MPCFill #62", "1bN_tTrxQ_EejHOg23JvJl3KpO_17Ia07"),
    ("MPCFill #63", "1hTsWY8cV1XR7cfj-q-nFawVNzN_0fciG"),
    ("MPCFill #64", "1nw2OWnjneAb5RMbQlPjsWk6g0dj2CwEz"),
    ("MPCFill #65", "1qxpqY5EKCFVWsOFJsYS3nCti5jBnguG7"),
    ("MPCFill #66", "1L7lEr9VPE_rSvNEhO7fjexvTYbaztfnO"),
    ("MPCFill #67", "1zeLPLoBcZdC_sIhTUG9I3Uj00vmaR2jv"),
]


def _catalog_url(drive_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{drive_id}"


def _catalog_as_dicts() -> list[dict[str, str | bool]]:
    """Serializa el catálogo curado con URL construida y tags por defecto."""
    return [
        {
            "name": name,
            "url": _catalog_url(fid),
            "source_type": "gdrive",
            "description": "Drive comunitario indexado por mpcfill.com",
            "tags": "mpcfill",
            "pinned": False,
        }
        for name, fid in _CURATED_CATALOG
    ]


_GDRIVE_FOLDER_RE = re.compile(r"drive\.google\.com/drive/folders/([A-Za-z0-9_\-]+)")
_GDRIVE_FILE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([A-Za-z0-9_\-]+)")


@dataclass
class ParsedGoogleDriveUrl:
    kind: str          # "folder" | "file" | "unknown"
    id: str | None     # drive/file id extraído
    canonical: str     # URL canónica (para folder), o dirección de descarga (file)


def parse_gdrive_url(url: str) -> ParsedGoogleDriveUrl:
    """Reconoce URLs de Google Drive de varios formatos y devuelve una forma
    canónica que la app puede usar (para folders → link a la UI; para files →
    URL de descarga directa 'uc?id=...&export=download').
    """
    if not url:
        return ParsedGoogleDriveUrl("unknown", None, url)
    m = _GDRIVE_FOLDER_RE.search(url)
    if m:
        fid = m.group(1)
        return ParsedGoogleDriveUrl(
            "folder", fid, f"https://drive.google.com/drive/folders/{fid}"
        )
    m = _GDRIVE_FILE_RE.search(url)
    if m:
        fid = m.group(1)
        return ParsedGoogleDriveUrl(
            "file", fid, f"https://drive.google.com/uc?id={fid}&export=download"
        )
    return ParsedGoogleDriveUrl("unknown", None, url)


async def list_sources(db: AsyncSession) -> list[ArtSource]:
    return list(
        (await db.scalars(
            select(ArtSource).order_by(ArtSource.pinned.desc(), ArtSource.name)
        )).all()
    )


def _detect_source_type(url: str) -> tuple[str, str]:
    """Detecta el ``source_type`` a partir de la URL y devuelve
    ``(source_type, canonical_url)``.

    Reglas de detección (evaluadas en orden):
      1. Google Drive folder → "gdrive" + URL canónica
      2. Google Drive file → "gdrive-file" + URL de descarga directa
      3. Prefijo ``file://`` o ruta absoluta local → "local-folder"
      4. Termina en ``.json`` sobre HTTP(S) → "http-listing"
      5. Cualquier otra cosa → "other"

    NUEVO en Fase 2 (Tarea 7): antes solo distinguíamos gdrive vs
    gdrive-file vs other. Ahora reconocemos local-folder y http-listing.

    La detección es best-effort — el usuario puede sobreescribir el tipo
    desde la UI si le hace falta (por ejemplo, para un HTTP manifest cuya
    URL no termine en .json).
    """
    from mpc_forge.services.source_types import resolve
    from pathlib import Path
    raw = (url or "").strip()

    # 1-2) Google Drive
    parsed = parse_gdrive_url(raw)
    if parsed.kind == "folder":
        return "gdrive", parsed.canonical
    if parsed.kind == "file":
        return "gdrive-file", parsed.canonical

    # 3) Local folder — file:// o ruta absoluta detectable
    if raw.startswith("file://") or (len(raw) >= 2 and (raw[0] in "/\\" or raw[1] == ":")):
        try:
            local_cls = resolve("local-folder")
            if local_cls:
                canonical = local_cls.validate_url(raw)
                return "local-folder", canonical
        except (ValueError, Exception):  # noqa: BLE001
            # Ruta parece local pero no válida — cae a "other" para que el
            # usuario vea el warning en la UI y corrija.
            pass

    # 4) HTTP JSON manifest
    if raw.startswith(("http://", "https://")) and raw.lower().endswith(".json"):
        try:
            http_cls = resolve("http-listing")
            if http_cls:
                canonical = http_cls.validate_url(raw)
                return "http-listing", canonical
        except (ValueError, Exception):  # noqa: BLE001
            pass

    # 5) S3 / R2 (Extras · F3/T7). Heurística por prefijo o hostname.
    is_s3_like = (
        raw.startswith("s3://")
        or ".s3.amazonaws.com" in raw.lower()
        or ".r2.cloudflarestorage.com" in raw.lower()
        or ".r2.dev" in raw.lower()
    )
    if is_s3_like:
        try:
            s3_cls = resolve("s3")
            if s3_cls:
                canonical = s3_cls.validate_url(raw)
                return "s3", canonical
        except (ValueError, Exception):  # noqa: BLE001
            pass

    return "other", raw


async def add_source(
    db: AsyncSession,
    name: str,
    url: str,
    description: str = "",
    tags: str = "",
    pinned: bool = False,
    source_type: str | None = None,
) -> ArtSource:
    """Crea un ArtSource nuevo. Si ``source_type`` no se especifica, se
    autodetecta a partir de la URL (ver ``_detect_source_type``).
    """
    if not name.strip():
        raise ValueError("El nombre no puede estar vacío")
    if not url.strip():
        raise ValueError("La URL no puede estar vacía")

    if source_type:
        # Si el caller fuerza un tipo, respetamos su URL tal cual (después
        # de validate_url del tipo, que puede normalizar).
        from mpc_forge.services.source_types import resolve
        type_cls = resolve(source_type)
        if type_cls is None:
            raise ValueError(f"Tipo de source desconocido: {source_type!r}")
        canonical = type_cls.validate_url(url)
        src_type = source_type
    else:
        src_type, canonical = _detect_source_type(url)

    src = ArtSource(
        name=name.strip(),
        url=canonical,
        source_type=src_type,
        description=description.strip(),
        tags=tags.strip(),
        pinned=pinned,
    )
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return src


async def update_source(
    db: AsyncSession,
    source_id: int,
    name: str | None = None,
    url: str | None = None,
    description: str | None = None,
    tags: str | None = None,
    pinned: bool | None = None,
    source_type: str | None = None,
) -> ArtSource | None:
    src = await db.get(ArtSource, source_id)
    if not src:
        return None
    if name is not None:
        src.name = name.strip()
    if url is not None:
        if source_type:
            # Tipo forzado por el caller — validar contra su clase concreta.
            from mpc_forge.services.source_types import resolve
            type_cls = resolve(source_type)
            if type_cls is None:
                raise ValueError(f"Tipo de source desconocido: {source_type!r}")
            src.url = type_cls.validate_url(url)
            src.source_type = source_type
        else:
            src.source_type, src.url = _detect_source_type(url)
    elif source_type is not None:
        # Solo cambia el tipo (URL no tocada): validamos con el nuevo tipo.
        from mpc_forge.services.source_types import resolve
        type_cls = resolve(source_type)
        if type_cls is None:
            raise ValueError(f"Tipo de source desconocido: {source_type!r}")
        src.url = type_cls.validate_url(src.url)
        src.source_type = source_type
    if description is not None:
        src.description = description.strip()
    if tags is not None:
        src.tags = tags.strip()
    if pinned is not None:
        src.pinned = pinned
    await db.commit()
    await db.refresh(src)
    return src


async def delete_source(db: AsyncSession, source_id: int) -> bool:
    src = await db.get(ArtSource, source_id)
    if not src:
        return False
    await db.delete(src)
    await db.commit()
    return True


async def seed_initial_if_empty(db: AsyncSession) -> int:
    """Al arrancar por primera vez, si la tabla está vacía, insertamos el
    catálogo curado completo (67 drives de MPCFill).
    """
    existing = (await db.scalars(select(ArtSource))).first()
    if existing:
        return 0
    for s in _catalog_as_dicts():
        db.add(ArtSource(
            name=str(s["name"]),
            url=str(s["url"]),
            source_type=str(s["source_type"]),
            description=str(s["description"]),
            tags=str(s["tags"]),
            pinned=bool(s["pinned"]),
        ))
    await db.commit()
    return len(_CURATED_CATALOG)


async def restore_catalog(db: AsyncSession) -> dict[str, int]:
    """Añade al catálogo cualquier drive del catálogo curado que el usuario
    haya borrado. NO toca los drives que el usuario haya añadido a mano
    ni renombra los que ya existen. Idempotente.

    Returns dict con {added, skipped, total_curated}.
    """
    # Índice de URLs existentes (ignoramos casing y trailing slashes)
    existing_rows = (await db.scalars(select(ArtSource))).all()
    existing_urls = {(s.url or "").rstrip("/").lower() for s in existing_rows}

    added = 0
    skipped = 0
    for s in _catalog_as_dicts():
        url_norm = str(s["url"]).rstrip("/").lower()
        if url_norm in existing_urls:
            skipped += 1
            continue
        db.add(ArtSource(
            name=str(s["name"]),
            url=str(s["url"]),
            source_type=str(s["source_type"]),
            description=str(s["description"]),
            tags=str(s["tags"]),
            pinned=bool(s["pinned"]),
        ))
        added += 1
    if added:
        await db.commit()
    return {"added": added, "skipped": skipped, "total_curated": len(_CURATED_CATALOG)}


def catalog_size() -> int:
    """Nº total de drives del catálogo curado (para mostrar en UI)."""
    return len(_CURATED_CATALOG)


def to_download_url(url: str) -> str:
    """Si es una URL de Google Drive de archivo, devuelve la URL de descarga
    directa. Si no, devuelve la URL original.
    """
    parsed = parse_gdrive_url(url)
    if parsed.kind == "file" and parsed.id:
        return f"https://drive.google.com/uc?id={parsed.id}&export=download"
    return url
