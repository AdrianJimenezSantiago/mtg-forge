from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from pathlib import Path
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
