"""Custom art local: indexa la carpeta `custom_art/` y matchea por nombre de carta.

Convenciones de nombrado (dropea archivos con el nombre de la carta):
  * `Sol Ring.png`                       → card="sol ring", front, sin variant
  * `Sol Ring - Anime.png`               → card="sol ring", front, variant="Anime"
  * `Sol Ring (Retro Frame).png`         → card="sol ring", front, variant="Retro Frame"
  * `Delver of Secrets [BACK].png`       → card="delver of secrets", back
  * `Delver of Secrets [BACK] - v2.png`  → card="delver of secrets", back, variant="v2"

Subcarpetas: se recorren recursivamente. El nombre de subcarpeta no importa
para el match — puedes organizar tus artes por juego, artista, etc.

También soporta añadir por URL: se descarga a la carpeta bajo `_downloaded/`.
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import httpx
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.config import PATHS
from mpc_forge.models import CustomArt
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_BACK_MARK_RE = re.compile(r"\[\s*(back|b)\s*\]", re.IGNORECASE)
_VARIANT_DASH_RE = re.compile(r"\s+-\s+(.+)$")
_VARIANT_PAREN_RE = re.compile(r"\s*\((.+?)\)\s*$")

# Caracteres que rompen URLs (#, ?) o son inválidos en filesystems Windows
# (\ / : * " < > |). Los reemplazamos por variantes seguras que preservan la
# legibilidad del nombre.
_UNSAFE_FILENAME_CHARS = {
    "#": "",       # elimina — evita fragment identifier en URLs
    "?": "",       # elimina — evita query string en URLs
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
    """Devuelve `name` sin caracteres que rompan URLs o filesystems.

    Ejemplo: "Atraxa - MPCFill #02 (2)" → "Atraxa - MPCFill 02 (2)".
    Colapsa espacios múltiples que pudieran resultar del reemplazo.
    """
    out = name
    for bad, good in _UNSAFE_FILENAME_CHARS.items():
        out = out.replace(bad, good)
    # Colapsar espacios y quitar puntos/espacios al final (Windows los pierde)
    out = re.sub(r"\s+", " ", out).strip(" .")
    return out or "arte"


DOWNLOADED_SUBDIR = "_downloaded"


def custom_art_url(relative_path: str) -> str:
    """Construye una URL segura a /custom_art/... a partir del relative_path.

    Usa urllib.parse.quote() para escapar caracteres que rompen URLs (`#`, `?`,
    espacios, etc.). Sin esto, un filename como "Atraxa - MPCFill #02.jpg"
    quedaría cortado en '#' porque el navegador interpreta lo posterior como
    fragment identifier y no lo envía al servidor.

    `safe="/"` preserva las barras de separación de directorios pero escapa
    todo lo demás.
    """
    return f"/custom_art/{quote(relative_path, safe='/')}"


def normalize_card_name(name: str) -> str:
    """lowercase, trim, colapsa espacios, apóstrofes tipográficos → simples."""
    n = name.strip().lower()
    n = n.replace("’", "'").replace("`", "'")
    n = re.sub(r"\s+", " ", n)
    return n


def parse_filename(rel_path: Path) -> tuple[str, str, str | None]:
    """Extrae (card_name_normalized, face, variant_label) del stem del archivo."""
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


async def rescan(db: AsyncSession) -> dict[str, int]:
    """Reindexa toda la carpeta. Añade nuevos, elimina huérfanos.

    Devuelve stats: {"total", "added", "removed", "kept"}.
    """
    root = PATHS.custom_art_dir
    disk_files: dict[str, Path] = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _IMAGE_EXTS:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        disk_files[rel] = f

    existing = (await db.scalars(select(CustomArt))).all()
    existing_by_path: dict[str, CustomArt] = {ca.relative_path: ca for ca in existing}

    added = 0
    removed = 0
    kept = 0

    for rel_path, ca in existing_by_path.items():
        if rel_path not in disk_files:
            await db.delete(ca)
            removed += 1

    for rel_path, abs_path in disk_files.items():
        if rel_path in existing_by_path:
            kept += 1
            continue
        card_name, face, variant = parse_filename(Path(rel_path))
        db.add(CustomArt(
            filename=Path(rel_path).name,
            relative_path=rel_path,
            card_name_normalized=card_name,
            variant_label=variant,
            face=face,
            bytes_size=abs_path.stat().st_size,
        ))
        added += 1

    await db.commit()
    return {"total": len(disk_files), "added": added, "removed": removed, "kept": kept}


async def find_for_card(
    db: AsyncSession, card_name: str, face: str = "front"
) -> list[CustomArt]:
    """Devuelve los custom arts que coinciden con el nombre de carta y cara."""
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
    return (PATHS.custom_art_dir / art.relative_path).resolve()


async def add_from_url(
    db: AsyncSession,
    url: str,
    card_name: str,
    face: str = "front",
    variant: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> CustomArt:
    """Descarga una imagen de una URL y la añade como custom art.

    Guarda en `custom_art/_downloaded/<slug>/<filename>` para no mezclar con los
    archivos que el usuario dropea a mano. El nombre de carta se usa tal cual
    (el filename resultante contendrá el nombre para futuras reindexaciones).

    Reconoce URLs de Google Drive de varios formatos y las convierte a la URL de
    descarga directa: `https://drive.google.com/uc?id=<FILE_ID>&export=download`.
    """
    from mpc_forge.services.art_sources import to_download_url
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

    # Deducir extensión del content-type o URL.
    ext = _guess_extension(url, resp.headers.get("content-type", ""))
    if ext.lower() not in _IMAGE_EXTS:
        raise ValueError(f"La URL no devuelve una imagen soportada (ext={ext})")

    # Nombre de archivo con la convención de la app: "Card Name [BACK] - Variant.ext"
    base = card_name.strip()
    if face == "back":
        base += " [BACK]"
    if variant:
        base += f" - {variant}"
    # Saneamos caracteres problemáticos:
    #   - '#' rompe URLs (todo lo que va después se interpreta como fragment)
    #   - '?' rompe URLs (empieza query string)
    #   - '\\/:*"<>|' son inválidos en Windows filesystems
    # Los reemplazamos por variantes seguras que preservan la legibilidad.
    base = _sanitize_filename(base)
    filename = f"{base}{ext}"

    # Subcarpeta bajo _downloaded para que el usuario sepa qué añadió por URL.
    subdir = PATHS.custom_art_dir / DOWNLOADED_SUBDIR
    subdir.mkdir(parents=True, exist_ok=True)

    # Evitar colisiones si ya existe:
    target = subdir / filename
    n = 1
    while target.exists():
        target = subdir / f"{base} ({n}){ext}"
        n += 1

    target.write_bytes(data)
    rel = str(target.relative_to(PATHS.custom_art_dir)).replace("\\", "/")
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
    """Deducir extensión desde content-type primero, URL después."""
    if content_type:
        primary = content_type.split(";", 1)[0].strip().lower()
        guessed = mimetypes.guess_extension(primary)
        if guessed:
            # normaliza .jpe → .jpg
            if guessed == ".jpe":
                return ".jpg"
            return guessed
    path = urlparse(url).path
    stem = Path(unquote(path)).suffix
    if stem:
        return stem
    return ".jpg"
