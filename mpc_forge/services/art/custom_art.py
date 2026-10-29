from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import mimetypes
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any
from urllib.parse import quote, unquote, urlparse

import httpx
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import ArtThemeEntry, CustomArt, CustomArtFolder, Deck, DeckCard
from mpc_forge.services.art.name_matching import card_keys, match_key
from mpc_forge.services.indexing.art_sources import to_download_url
from mpc_forge.ssl_config import ssl_insecure
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_BACK_MARK_RE = re.compile(r"\[\s*(back|b)\s*\]", re.IGNORECASE)
_BRACKET_TAG_RE = re.compile(r"\s*\[([^\]]*)\]\s*")
_VARIANT_DASH_RE = re.compile(r"\s+-\s+(.+)$")
_VARIANT_PAREN_RE = re.compile(r"\s*\((.+?)\)\s*$")
_NUMBERING_RE = re.compile(r"^(?:0\d{0,3}\s+|\d{1,4}\s*[-_.)]\s*|\(\d{1,4}\)\s*)(?=\S)")

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
    return match_key(name)


def parse_filename(rel_path: Path) -> tuple[str, str, str | None]:
    """Saca (clave de carta, cara, variante) del nombre de un fichero.

    Entiende `Sol Ring.png`, `Sol Ring - Anime.png`, `Sol Ring (Artista).png`,
    `Delver of Secrets [BACK].png`, etiquetas como `[SLD]`, guiones bajos en
    lugar de espacios y numeraciones delante (`001 Sol Ring`, `12. Sol Ring`).
    """
    stem = rel_path.stem.replace("_", " ").strip() or rel_path.stem

    face = "front"
    if _BACK_MARK_RE.search(stem):
        face = "back"
        stem = _BACK_MARK_RE.sub(" ", stem).strip()

    labels: list[str] = []
    tags = [t.strip() for t in _BRACKET_TAG_RE.findall(stem) if t.strip()]
    if tags:
        stem = _BRACKET_TAG_RE.sub(" ", stem).strip()
    stem = _NUMBERING_RE.sub("", stem, count=1).strip() or stem

    m = _VARIANT_PAREN_RE.search(stem)
    if m:
        labels.append(m.group(1).strip())
        stem = stem[: m.start()].strip()
    else:
        m = _VARIANT_DASH_RE.search(stem)
        if m:
            labels.append(m.group(1).strip())
            stem = stem[: m.start()].strip()
    labels.extend(tags)

    variant = " ".join(labels).strip() or None
    return normalize_card_name(stem), face, variant


LINKED_PREFIX = "_linked"
MAX_FILES_PER_ROOT = 50_000
_IGNORED_DIRS = frozenset(
    {"__pycache__", "node_modules", "$RECYCLE.BIN", "System Volume Information", "__MACOSX"}
)

_linked_roots: dict[int, Path] = {}
_rescan_lock = asyncio.Lock()


def linked_relative_path(folder_id: int, relative: str) -> str:
    return f"{LINKED_PREFIX}/{folder_id}/{relative}"


def linked_folder_id(relative_path: str) -> int | None:
    head, _, rest = relative_path.partition("/")
    if head != LINKED_PREFIX:
        return None
    folder, _, _ = rest.partition("/")
    return int(folder) if folder.isdigit() else None


def is_linked(art: CustomArt) -> bool:
    return linked_folder_id(art.relative_path) is not None


def set_linked_roots(roots: dict[int, Path]) -> None:
    _linked_roots.clear()
    _linked_roots.update(roots)


async def load_linked_roots(db: AsyncSession) -> dict[int, Path]:
    rows = (await db.execute(select(CustomArtFolder.id, CustomArtFolder.path))).all()
    roots = {folder_id: Path(path) for folder_id, path in rows}
    set_linked_roots(roots)
    return roots


def resolve_relative(relative_path: str) -> Path | None:
    """Ruta en disco de un `relative_path` de CustomArt, o None si no es válido.

    Los ficheros de carpetas vinculadas viven bajo `_linked/<id>/…`; el resto,
    en la carpeta de arte custom. Nunca devuelve una ruta fuera de su raíz.
    """
    rel = relative_path.replace("\\", "/").lstrip("/")
    folder_id = linked_folder_id(rel)
    if folder_id is not None:
        root = _linked_roots.get(folder_id)
        if root is None:
            return None
        rel = rel.split("/", 2)[2] if rel.count("/") >= 2 else ""
    elif rel.split("/", 1)[0] == LINKED_PREFIX:
        return None
    else:
        root = cfg.PATHS.custom_art_dir
    if not rel:
        return None
    try:
        base = root.resolve()
        candidate = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_relative_to(base) else None


def _is_ignored(parts: tuple[str, ...]) -> bool:
    return any(p.startswith(".") or p in _IGNORED_DIRS for p in parts)


def is_servable(relative: str) -> bool:
    """Si una ruta dentro de una raíz de arte custom se puede servir al navegador."""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    return bool(parts) and not _is_ignored(parts) and Path(parts[-1]).suffix.lower() in _IMAGE_EXTS


def _scan_disk(
    root: Path, *, skip_top: frozenset[str] = frozenset()
) -> tuple[dict[str, int], bool]:
    """Imágenes bajo `root` como ({ruta relativa POSIX: bytes}, escaneo completo).

    Ignora carpetas ocultas o de sistema y se detiene en `MAX_FILES_PER_ROOT`
    para que vincular por error una carpeta enorme no bloquee la app; en ese
    caso el escaneo no está completo y lo que falte no se da por borrado.
    """
    disk: dict[str, int] = {}
    for f in root.rglob("*"):
        try:
            rel_parts = f.relative_to(root).parts
            if not rel_parts or rel_parts[0] in skip_top or _is_ignored(rel_parts):
                continue
            if f.suffix.lower() not in _IMAGE_EXTS or not f.is_file():
                continue
            size = f.stat().st_size
        except OSError:
            continue
        disk["/".join(rel_parts)] = size
        if len(disk) >= MAX_FILES_PER_ROOT:
            log.warning("%s tiene más de %d imágenes; se indexan solo esas", root, len(disk))
            return disk, False
    return disk, True


async def detach_references(db: AsyncSession, art_ids: list[int]) -> None:
    """Quita las referencias a artes que se van a borrar.

    SQLite no aplica las claves foráneas de este esquema, y un id borrado se
    puede reutilizar: sin esto una carta acabaría mostrando otra imagen.
    """
    for chunk in chunked(art_ids):
        for model, column in (
            (DeckCard, DeckCard.custom_art_front_id),
            (DeckCard, DeckCard.custom_art_back_id),
            (Deck, Deck.custom_cardback_art_id),
            (ArtThemeEntry, ArtThemeEntry.custom_art_front_id),
            (ArtThemeEntry, ArtThemeEntry.custom_art_back_id),
        ):
            await db.execute(update(model).where(column.in_(chunk)).values({column.key: None}))


async def delete_arts(db: AsyncSession, art_ids: list[int]) -> None:
    if not art_ids:
        return
    await detach_references(db, art_ids)
    for chunk in chunked(art_ids):
        await db.execute(delete(CustomArt).where(CustomArt.id.in_(chunk)))


async def rescan(db: AsyncSession) -> dict[str, int]:
    """Sincroniza el índice con la carpeta de arte custom y las vinculadas.

    Una carpeta vinculada que no está disponible (un disco externo
    desconectado) conserva sus entradas: así no se pierden las asignaciones
    de los mazos hasta que vuelva.
    """
    async with _rescan_lock:
        return await _rescan(db)


async def _rescan(db: AsyncSession) -> dict[str, int]:
    roots = await load_linked_roots(db)
    disk_files, main_complete = await asyncio.to_thread(
        _scan_disk, cfg.PATHS.custom_art_dir, skip_top=frozenset({LINKED_PREFIX})
    )

    offline: set[int] = set()
    partial: set[int | None] = set() if main_complete else {None}
    per_folder: dict[int, int] = {}
    for folder_id, root in roots.items():
        if not await asyncio.to_thread(root.is_dir):
            offline.add(folder_id)
            continue
        files, complete = await asyncio.to_thread(_scan_disk, root)
        if not complete:
            partial.add(folder_id)
        per_folder[folder_id] = len(files)
        for rel, size in files.items():
            disk_files[linked_relative_path(folder_id, rel)] = size

    existing = (await db.scalars(select(CustomArt))).all()
    existing_by_path: dict[str, CustomArt] = {ca.relative_path: ca for ca in existing}

    orphan_ids = [
        ca.id
        for path, ca in existing_by_path.items()
        if path not in disk_files
        and linked_folder_id(path) not in offline
        and linked_folder_id(path) not in partial
    ]
    await delete_arts(db, orphan_ids)

    added = 0
    kept = 0
    for rel_path, size in disk_files.items():
        current = existing_by_path.get(rel_path)
        card_name, face, variant = parse_filename(Path(rel_path))
        if current is not None:
            kept += 1
            if current.card_name_normalized != card_name:
                current.card_name_normalized = card_name
            if current.bytes_size != size:
                current.bytes_size = size
            continue
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

    if per_folder:
        now = datetime.now(UTC)
        for folder in (
            await db.scalars(select(CustomArtFolder).where(CustomArtFolder.id.in_(per_folder)))
        ).all():
            folder.file_count = per_folder[folder.id]
            folder.last_scanned_at = now

    await db.commit()
    return {
        "total": len(disk_files),
        "added": added,
        "removed": len(orphan_ids),
        "kept": kept,
        "offline_folders": len(offline),
    }


async def find_for_card(db: AsyncSession, card_name: str, face: str = "front") -> list[CustomArt]:
    """Artes custom de una cara de la carta.

    El frontal acepta el nombre completo y el de la cara frontal. El reverso
    acepta los ficheros marcados `[BACK]` y, en cartas de dos caras, los que se
    llaman como la cara trasera.
    """
    keys = card_keys(card_name)
    if face == "back":
        condition = or_(
            and_(CustomArt.face == "back", CustomArt.card_name_normalized.in_(keys.front)),
            and_(CustomArt.face == "front", CustomArt.card_name_normalized.in_(keys.back)),
        )
    else:
        condition = and_(CustomArt.face == face, CustomArt.card_name_normalized.in_(keys.front))
    rows = (await db.scalars(select(CustomArt).where(condition).order_by(CustomArt.filename))).all()
    return list(rows)


def absolute_path(art: CustomArt) -> Path | None:
    return resolve_relative(art.relative_path)


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
        if path is None or art.bytes_size != len(data):
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
    """Borra el fichero de un arte y la carpeta de subida si queda vacía.

    Nunca toca los ficheros de una carpeta vinculada: son del usuario.
    """
    path = absolute_path(art)
    if path is None or is_linked(art):
        return
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


IMPORTED_SUBDIR = "_imported"
MAX_ZIP_ENTRIES = 3000
MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 50


@dataclass
class ZipImportResult:
    art_ids: list[int] = field(default_factory=list)
    added: int = 0
    duplicates: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "art_ids": self.art_ids,
            "added": self.added,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
        }


def _zip_image_entries(zf: zipfile.ZipFile, result: ZipImportResult) -> list[zipfile.ZipInfo]:
    """Entradas de imagen del ZIP, tras descartar lo que no se debe extraer."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UploadRejected(
            "zip_too_many_entries", f"El ZIP tiene más de {MAX_ZIP_ENTRIES} ficheros."
        )
    if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL_BYTES:
        raise UploadRejected("zip_too_large", "El ZIP descomprimido ocupa demasiado.", 413)

    out: list[zipfile.ZipInfo] = []
    for info in infos:
        name = PurePosixPath(info.filename.replace("\\", "/"))
        folders = tuple(p for p in name.parts[:-1] if p not in (".", ".."))
        if info.is_dir() or _is_ignored(folders) or name.name.startswith("."):
            continue
        if name.suffix.lower() not in _IMAGE_EXTS:
            result.skipped.append({"name": name.name, "code": "unsupported_format"})
            continue
        if info.flag_bits & 0x1:
            result.skipped.append({"name": name.name, "code": "encrypted"})
        elif info.file_size > MAX_UPLOAD_BYTES:
            result.skipped.append({"name": name.name, "code": "too_large"})
        elif info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            result.skipped.append({"name": name.name, "code": "suspicious"})
        else:
            out.append(info)
    return out


def _read_and_prepare(zf: zipfile.ZipFile, info: zipfile.ZipInfo, fit: str) -> PreparedImage:
    with zf.open(info) as fh:
        data = fh.read(MAX_UPLOAD_BYTES + 1)
    return prepare_upload(data, fit)


def _free_target(folder: Path, stem: str, ext: str) -> Path:
    target = folder / f"{stem}{ext}"
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){ext}"
        n += 1
    return target


async def import_zip(
    db: AsyncSession, fileobj: IO[bytes], archive_name: str, *, fit: str = "stretch"
) -> ZipImportResult:
    """Extrae las imágenes de un ZIP a la biblioteca de arte custom.

    Cada imagen pasa por las mismas comprobaciones que una subida suelta y
    conserva su nombre, que es lo que decide a qué carta corresponde. Se
    extraen de una en una para no tener el ZIP entero en memoria.
    """
    if fit not in FIT_MODES:
        raise UploadRejected("invalid_fit", f"Modo de ajuste desconocido: {fit!r}")
    result = ZipImportResult()
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError) as e:
        raise UploadRejected("bad_zip", "El fichero no es un ZIP válido.") from e

    root = cfg.PATHS.custom_art_dir
    folder = root / IMPORTED_SUBDIR / _sanitize_filename(Path(archive_name).stem or "zip")
    with zf:
        entries = await asyncio.to_thread(_zip_image_entries, zf, result)
        for info in entries:
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            try:
                prepared = await asyncio.to_thread(_read_and_prepare, zf, info, fit)
            except UploadRejected as e:
                result.skipped.append({"name": name, "code": e.code})
                continue
            except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
                result.skipped.append({"name": name, "code": "corrupt"})
                continue

            key, face, variant = parse_filename(Path(name))
            existing = await find_duplicate(db, key, face, prepared.data)
            if existing is not None:
                result.duplicates += 1
                result.art_ids.append(existing.id)
                continue

            folder.mkdir(parents=True, exist_ok=True)
            target = _free_target(folder, _sanitize_filename(Path(name).stem), prepared.ext)
            await asyncio.to_thread(target.write_bytes, prepared.data)
            art = CustomArt(
                filename=target.name,
                relative_path=target.relative_to(root).as_posix(),
                card_name_normalized=key,
                variant_label=variant,
                face=face,
                bytes_size=len(prepared.data),
            )
            db.add(art)
            await db.flush()
            result.art_ids.append(art.id)
            result.added += 1

    await db.commit()
    log.info(
        "ZIP %s importado: %d nuevas, %d repetidas, %d descartadas",
        archive_name,
        result.added,
        result.duplicates,
        len(result.skipped),
    )
    return result
