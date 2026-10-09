from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import CustomArt
from mpc_forge.services.indexing.art_sources import to_download_url
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_BACK_MARK_RE = re.compile(r"\[\s*(back|b)\s*\]", re.IGNORECASE)
_VARIANT_DASH_RE = re.compile(r"\s+-\s+(.+)$")
_VARIANT_PAREN_RE = re.compile(r"\s*\((.+?)\)\s*$")

_UNSAFE_FILENAME_CHARS = {
    "#": "",
    "?": "",
    "\\": "-",
    "/": "-",
    ":": "-",
    "*": "",
    '"': "",
    "<": "",
    ">": "",
    "|": "-",
}


def _sanitize_filename(name: str) -> str:
    out = name
    for bad, good in _UNSAFE_FILENAME_CHARS.items():
        out = out.replace(bad, good)
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out or "arte"


DOWNLOADED_SUBDIR = "_downloaded"
UPLOADED_SUBDIR = "_uploaded"


def custom_art_url(relative_path: str) -> str:
    return f"/custom_art/{quote(relative_path, safe='/')}"


def normalize_card_name(name: str) -> str:
    n = name.strip().lower()
    n = n.replace("’", "'").replace("`", "'")
    n = re.sub(r"\s+", " ", n)
    return n


def parse_filename(rel_path: Path) -> tuple[str, str, str | None]:
    stem = rel_path.stem

    face = "front"
    m = _BACK_MARK_RE.search(stem)
    if m:
        face = "back"
        stem = _BACK_MARK_RE.sub("", stem).strip()

    variant: str | None = None
    m = _VARIANT_PAREN_RE.search(stem)
    if m:
        variant = m.group(1).strip()
        stem = _VARIANT_PAREN_RE.sub("", stem).strip()
    else:
        m = _VARIANT_DASH_RE.search(stem)
        if m:
            variant = m.group(1).strip()
            stem = _VARIANT_DASH_RE.sub("", stem).strip()

    return normalize_card_name(stem), face, variant


def _scan_disk(root: Path) -> dict[str, tuple[Path, int]]:
    disk: dict[str, tuple[Path, int]] = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _IMAGE_EXTS:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        disk[rel] = (f, size)
    return disk


async def rescan(db: AsyncSession) -> dict[str, int]:
    root = cfg.PATHS.custom_art_dir
    disk_files = await asyncio.to_thread(_scan_disk, root)

    existing = (await db.scalars(select(CustomArt))).all()
    existing_by_path: dict[str, CustomArt] = {ca.relative_path: ca for ca in existing}

    orphan_ids = [ca.id for path, ca in existing_by_path.items() if path not in disk_files]
    removed = len(orphan_ids)
    if orphan_ids:
        await db.execute(delete(CustomArt).where(CustomArt.id.in_(orphan_ids)))

    added = 0
    kept = 0
    for rel_path, (_abs_path, size) in disk_files.items():
        if rel_path in existing_by_path:
            kept += 1
            continue
        card_name, face, variant = parse_filename(Path(rel_path))
        db.add(
            CustomArt(
                filename=Path(rel_path).name,
                relative_path=rel_path,
                card_name_normalized=card_name,
                variant_label=variant,
                face=face,
                bytes_size=size,
            )
        )
        added += 1

    await db.commit()
    return {"total": len(disk_files), "added": added, "removed": removed, "kept": kept}


async def find_for_card(db: AsyncSession, card_name: str, face: str = "front") -> list[CustomArt]:
    normalized = normalize_card_name(card_name)
    rows = (
        await db.scalars(
            select(CustomArt)
            .where(
                CustomArt.card_name_normalized == normalized,
                CustomArt.face == face,
            )
            .order_by(CustomArt.filename)
        )
    ).all()
    return list(rows)


def absolute_path(art: CustomArt) -> Path:
    return (cfg.PATHS.custom_art_dir / art.relative_path).resolve()


async def add_from_url(
    db: AsyncSession,
    url: str,
    card_name: str,
    face: str = "front",
    variant: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> CustomArt:
    url = to_download_url(url.strip())

    close_client = client is None
    client = client or httpx.AsyncClient(
        timeout=60.0, follow_redirects=True, verify=not ssl_insecure()
    )
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
    finally:
        if close_client:
            await client.aclose()

    ext = _guess_extension(url, resp.headers.get("content-type", ""))
    if ext.lower() not in _IMAGE_EXTS:
        raise ValueError(f"La URL no devuelve una imagen soportada (ext={ext})")

    base = card_name.strip()
    if face == "back":
        base += " [BACK]"
    if variant:
        base += f" - {variant}"
    base = _sanitize_filename(base)
    filename = f"{base}{ext}"

    subdir = cfg.PATHS.custom_art_dir / DOWNLOADED_SUBDIR
    subdir.mkdir(parents=True, exist_ok=True)

    target = subdir / filename
    n = 1
    while target.exists():
        target = subdir / f"{base} ({n}){ext}"
        n += 1

    target.write_bytes(data)
    rel = str(target.relative_to(cfg.PATHS.custom_art_dir)).replace("\\", "/")
    normalized = normalize_card_name(card_name)

    art = CustomArt(
        filename=target.name,
        relative_path=rel,
        card_name_normalized=normalized,
        variant_label=variant,
        face=face,
        bytes_size=len(data),
    )
    db.add(art)
    await db.commit()
    await db.refresh(art)
    log.info("Custom art añadido: %s (%s bytes)", rel, len(data))
    return art


def _guess_extension(url: str, content_type: str) -> str:
    if content_type:
        primary = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(primary)
        if guessed:
            if guessed == ".jpe":
                return ".jpg"
            return guessed
    path = urlparse(url).path
    stem = Path(unquote(path)).suffix
    if stem:
        return stem
    return ".jpg"


# --- Subida desde el navegador -------------------------------------------------
#
# El PDF pinta cada imagen estirada a 63x88 mm (preserveAspectRatio=False), así
# que lo que más importa validar es que la imagen sea de verdad una imagen, que
# tenga resolución suficiente y que su proporción sea la de una carta. Las
# imágenes con sangrado de MPC (p. ej. 822x1122) también se consideran válidas.

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_UPLOAD_PIXELS = 60_000_000
MIN_UPLOAD_SIDE_PX = 200
CARD_RATIO = 63 / 88
RATIO_MIN = 0.70
RATIO_MAX = 0.745
CARD_WIDTH_IN = 63 / 25.4
RECOMMENDED_DPI = 300
LOW_DPI = 240
FIT_MODES = ("stretch", "crop", "contain")
MAX_VARIANT_LEN = 60

_FORMAT_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_EXIF_ORIENTATION = 0x0112


class UploadRejected(ValueError):
    """La imagen subida no se puede usar. `code` es estable para la interfaz."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class PreparedImage:
    data: bytes
    ext: str
    width: int
    height: int
    source_width: int
    source_height: int
    adjusted: bool
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dpi(self) -> int:
        return round(self.width / CARD_WIDTH_IN)


def ratio_is_card_like(width: int, height: int) -> bool:
    return height > 0 and RATIO_MIN <= width / height <= RATIO_MAX


def _fit_to_card(img: Any, fit: str) -> Any:
    from PIL import Image

    w, h = img.size
    if fit == "crop":
        if w / h > CARD_RATIO:
            new_w = max(1, round(h * CARD_RATIO))
            left = (w - new_w) // 2
            return img.crop((left, 0, left + new_w, h))
        new_h = max(1, round(w / CARD_RATIO))
        top = (h - new_h) // 2
        return img.crop((0, top, w, top + new_h))
    if fit == "contain":
        if w / h > CARD_RATIO:
            canvas_w, canvas_h = w, max(1, round(w / CARD_RATIO))
        else:
            canvas_w, canvas_h = max(1, round(h * CARD_RATIO)), h
        has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
        mode = "RGBA" if has_alpha else "RGB"
        fill = (0, 0, 0, 255) if has_alpha else (0, 0, 0)
        canvas = Image.new(mode, (canvas_w, canvas_h), fill)
        src = img.convert(mode)
        canvas.paste(src, ((canvas_w - w) // 2, (canvas_h - h) // 2))
        return canvas
    return img


def _encode(img: Any, fmt: str) -> bytes:
    out = io.BytesIO()
    if fmt == "JPEG":
        if img.mode not in ("RGB", "L", "CMYK"):
            img = img.convert("RGB")
        img.save(out, "JPEG", quality=95, subsampling=0, optimize=True)
    elif fmt == "WEBP":
        img.save(out, "WEBP", quality=95, method=4)
    else:
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        img.save(out, "PNG", optimize=False)
    return out.getvalue()


def _has_transparency(img: Any) -> bool:
    if img.mode in ("RGBA", "LA"):
        extrema = img.getchannel("A").getextrema()
        return bool(extrema and extrema[0] < 255)
    return img.mode == "P" and "transparency" in img.info


def prepare_upload(data: bytes, fit: str = "stretch") -> PreparedImage:
    """Valida y, si hace falta, normaliza una imagen subida por el usuario.

    Rechaza lo que no se puede imprimir (no es imagen, formato no soportado,
    corrupta, diminuta o desproporcionadamente grande) y devuelve avisos para lo
    que se puede imprimir pero quizá no como espera el usuario.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if fit not in FIT_MODES:
        raise UploadRejected("invalid_fit", f"Modo de ajuste desconocido: {fit!r}")
    if not data:
        raise UploadRejected("empty", "El fichero está vacío.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(
            "too_large",
            f"La imagen pesa más de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            status_code=413,
        )

    try:
        probe = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError) as e:
        raise UploadRejected("not_an_image", "El fichero no es una imagen válida.") from e

    fmt = (probe.format or "").upper()
    if fmt not in _FORMAT_EXT:
        raise UploadRejected(
            "unsupported_format",
            f"Formato no soportado ({fmt or 'desconocido'}). Usa PNG, JPG o WEBP.",
        )
    if getattr(probe, "is_animated", False):
        raise UploadRejected("animated", "Las imágenes animadas no se pueden imprimir.")
    src_w, src_h = probe.size
    if src_w * src_h > MAX_UPLOAD_PIXELS:
        raise UploadRejected(
            "too_many_pixels",
            f"La imagen es demasiado grande ({src_w}x{src_h} px).",
            status_code=413,
        )
    if min(src_w, src_h) < MIN_UPLOAD_SIDE_PX:
        raise UploadRejected(
            "too_small",
            f"La imagen es demasiado pequeña ({src_w}x{src_h} px); "
            f"el mínimo es {MIN_UPLOAD_SIDE_PX} px por lado.",
        )

    try:
        probe.verify()
        decoded = Image.open(io.BytesIO(data))
        decoded.load()
    except Exception as e:
        raise UploadRejected("corrupt", "La imagen está dañada o incompleta.") from e

    img: Image.Image = decoded
    adjusted = False
    orientation = 1
    try:
        orientation = int(img.getexif().get(_EXIF_ORIENTATION, 1) or 1)
    except Exception:
        orientation = 1
    if orientation != 1:
        img = ImageOps.exif_transpose(img)
        adjusted = True

    if fit != "stretch" and not ratio_is_card_like(*img.size):
        img = _fit_to_card(img, fit)
        adjusted = True

    out = _encode(img, fmt) if adjusted else data
    width, height = img.size

    prepared = PreparedImage(
        data=out,
        ext=_FORMAT_EXT[fmt],
        width=width,
        height=height,
        source_width=src_w,
        source_height=src_h,
        adjusted=adjusted,
    )
    if not ratio_is_card_like(width, height):
        prepared.warnings.append(
            {"code": "aspect_mismatch", "ratio": round(width / height, 3), "expected": 0.716}
        )
    if prepared.dpi < LOW_DPI:
        prepared.warnings.append(
            {"code": "low_resolution", "dpi": prepared.dpi, "recommended": RECOMMENDED_DPI}
        )
    if _has_transparency(img):
        prepared.warnings.append({"code": "transparency"})
    return prepared


def _clean_variant(variant: str | None) -> str | None:
    if not variant:
        return None
    v = re.sub(r"[()\[\]]", " ", variant)
    v = _sanitize_filename(v)[:MAX_VARIANT_LEN].strip(" .-")
    return v or None


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def find_duplicate(
    db: AsyncSession, card_name: str, face: str, data: bytes
) -> CustomArt | None:
    digest = hashlib.sha256(data).hexdigest()
    for art in await find_for_card(db, card_name, face=face):
        path = absolute_path(art)
        if art.bytes_size != len(data):
            continue
        if await asyncio.to_thread(_file_digest, path) == digest:
            return art
    return None


async def _free_variant_label(
    db: AsyncSession, card_name: str, face: str, variant: str | None
) -> str | None:
    """Etiqueta que no repite ninguna variante ya guardada para esa carta y cara."""
    taken = {(a.variant_label or "").lower() for a in await find_for_card(db, card_name, face)}
    if (variant or "").lower() not in taken:
        return variant
    n = 2
    while True:
        label = f"{variant} {n}" if variant else str(n)
        if label.lower() not in taken:
            return label
        n += 1


def _write_upload(
    root: Path, card_name: str, face: str, label: str | None, ext: str, data: bytes
) -> Path:
    """Escribe la imagen con un nombre que `parse_filename` sabe releer.

    Cada contenido vive en su propia carpeta (`_uploaded/<hash>/`), así que una
    URL nunca apunta a dos imágenes distintas aunque se borre y se vuelva a
    subir otra con el mismo nombre: las cachés del navegador siguen siendo
    válidas y el nombre del fichero queda limpio.
    """
    digest = hashlib.sha256(data).hexdigest()[:12]
    base = card_name.strip()
    if face == "back":
        base += " [BACK]"
    if label:
        base += f" - {label}"
    folder = root / UPLOADED_SUBDIR / digest
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{_sanitize_filename(base)}{ext}"
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


def remove_file(art: CustomArt) -> None:
    """Borra el fichero de un arte y la carpeta de subida si queda vacía."""
    path = absolute_path(art)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
    uploads = (cfg.PATHS.custom_art_dir / UPLOADED_SUBDIR).resolve()
    parent = path.parent
    if parent != uploads and parent.is_relative_to(uploads):
        try:
            parent.rmdir()
        except OSError:
            pass


async def add_from_upload(
    db: AsyncSession,
    data: bytes,
    card_name: str,
    face: str = "front",
    variant: str | None = None,
    fit: str = "stretch",
) -> tuple[CustomArt, PreparedImage, bool]:
    """Guarda una imagen subida. Devuelve (arte, imagen preparada, era_duplicado)."""
    if face not in ("front", "back"):
        raise UploadRejected("invalid_face", "La cara debe ser 'front' o 'back'.")
    card_name = card_name.strip()
    if not card_name:
        raise UploadRejected("invalid_card", "Falta el nombre de la carta.")

    prepared = await asyncio.to_thread(prepare_upload, data, fit)

    existing = await find_duplicate(db, card_name, face, prepared.data)
    if existing is not None:
        return existing, prepared, True

    label = await _free_variant_label(db, card_name, face, _clean_variant(variant))
    target = await asyncio.to_thread(
        _write_upload,
        cfg.PATHS.custom_art_dir,
        card_name,
        face,
        label,
        prepared.ext,
        prepared.data,
    )
    rel = str(target.relative_to(cfg.PATHS.custom_art_dir)).replace("\\", "/")
    art = CustomArt(
        filename=target.name,
        relative_path=rel,
        card_name_normalized=normalize_card_name(card_name),
        variant_label=label,
        face=face,
        bytes_size=len(prepared.data),
    )
    db.add(art)
    try:
        await db.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    await db.refresh(art)
    log.info(
        "Custom art subido: %s (%sx%s px, %s bytes)",
        rel,
        prepared.width,
        prepared.height,
        len(prepared.data),
    )
    return art, prepared, False
