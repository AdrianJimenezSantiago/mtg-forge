"""Exporta las imágenes del mazo como ZIP para gente que las quiere sueltas
(p.ej. para MPCFill, para montar en otro editor de proxies, o para dárselas
a un impresor externo — el caso que motivó esta feature).

Contenido del ZIP:
- una imagen por cara única (frente + reverso DFC si aplica). El nombre del
  fichero es el nombre canónico de la carta según Scryfall, saneado para el
  sistema de archivos.
- ``decklist.txt`` en formato Moxfield-compatible con la lista completa.
- ``README.txt`` mínimo explicando cómo se usan los ficheros.

Naming DFC:
- Frente: <cara_frontal>.<ext>   (p.ej. "Delver of Secrets.png")
- Reverso: <cara_trasera>.<ext>  (p.ej. "Insectile Aberration.png")
  Si no hay back_name, cae a "<frente>__back.<ext>".

Se deduplican por path exacto para no meter la misma imagen 4 veces si
la carta aparece 4 veces en el mazo.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mpc_forge.services.xml_generator import DeckCardResolved

log = logging.getLogger(__name__)

# Caracteres inseguros en nombres de fichero cross-platform. Windows es el más
# restrictivo, así que respetamos su lista. También añadimos control chars.
_FORBIDDEN = set('/\\?*|"<>:')
_CONTROL = set(chr(i) for i in range(0, 32))


def _safe_filename(name: str, max_len: int = 120) -> str:
    """Sanea un nombre de carta para que sea un filename válido en Windows,
    macOS y Linux. Preserva casi todo (comas, apóstrofes, paréntesis, etc.)
    porque MPCFill hace matching por nombre y queremos conservar la forma
    original todo lo posible."""
    cleaned = ''.join('_' if (c in _FORBIDDEN or c in _CONTROL) else c for c in name)
    # Colapsa espacios múltiples y quita whitespace en los bordes.
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Windows además prohíbe puntos y espacios al final.
    cleaned = cleaned.rstrip('. ')
    if not cleaned:
        cleaned = 'unnamed'
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip('. ')
    return cleaned


def _split_dfc(full_name: str) -> tuple[str, str | None]:
    """Devuelve (front_face, back_face_or_None). Los nombres compuestos DFC
    de Scryfall vienen como "Delver of Secrets // Insectile Aberration"."""
    if ' // ' in full_name:
        front, back = full_name.split(' // ', 1)
        return front.strip(), back.strip()
    return full_name.strip(), None


@dataclass
class ImageExportResult:
    zip_path: Path
    total_files: int          # cuántos ficheros hay dentro del zip
    total_unique_cards: int   # cuántas cartas únicas (fronts)
    total_dfc_backs: int      # cuántos reversos DFC se incluyeron
    missing_images: int       # cuántas imágenes no se pudieron leer
    size_bytes: int


def build_images_zip(
    cards: list[DeckCardResolved],
    output_path: Path,
    decklist_text: str,
) -> ImageExportResult:
    """Construye el ZIP en ``output_path``. No baja imágenes; asume que las
    rutas de ``cards`` ya apuntan a ficheros existentes (mismo pipeline que
    ``build_pdf``: el caller llama antes a ``resolve_deck_for_xml``)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen_files: dict[str, str] = {}  # arcname → source path (dedupe)
    missing = 0
    total_backs = 0

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # --- Imágenes ---
        for c in cards:
            front_face, back_face = _split_dfc(c.name)
            ext = Path(str(c.front_path)).suffix or '.png'
            arc_front = f"{_safe_filename(front_face)}{ext}"

            # Dedupe por path exacto: si dos cartas comparten arte
            # (o si el nombre saneado colisiona), damos preferencia al primero.
            src = Path(str(c.front_path))
            if arc_front in seen_files and seen_files[arc_front] != str(src):
                # Colisión con arte distinto — desambigua con el scryfall_id.
                arc_front = f"{_safe_filename(front_face)}__{c.scryfall_id[:8]}{ext}"

            if arc_front not in seen_files:
                try:
                    zf.write(src, arcname=arc_front)
                    seen_files[arc_front] = str(src)
                except FileNotFoundError:
                    log.warning("Imagen de frente no encontrada: %s", src)
                    missing += 1

            # Cara trasera de DFC
            if c.back_path:
                back_name = c.back_name or back_face or f"{front_face}__back"
                back_ext = Path(str(c.back_path)).suffix or '.png'
                arc_back = f"{_safe_filename(back_name)}{back_ext}"

                back_src = Path(str(c.back_path))
                if arc_back in seen_files and seen_files[arc_back] != str(back_src):
                    arc_back = f"{_safe_filename(back_name)}__{c.scryfall_id[:8]}{back_ext}"

                if arc_back not in seen_files:
                    try:
                        zf.write(back_src, arcname=arc_back)
                        seen_files[arc_back] = str(back_src)
                        total_backs += 1
                    except FileNotFoundError:
                        log.warning("Imagen de reverso no encontrada: %s", back_src)
                        missing += 1

        # --- Decklist ---
        # UTF-8 sin BOM. Moxfield acepta ambos pero sin BOM es más portable.
        zf.writestr('decklist.txt', decklist_text)

        # --- README ---
        readme = _build_readme(len(cards), len([c for c in cards if c.back_path]))
        zf.writestr('README.txt', readme)

    total_files = len(seen_files) + 2  # + decklist.txt + README.txt
    return ImageExportResult(
        zip_path=output_path,
        total_files=total_files,
        total_unique_cards=len(cards),
        total_dfc_backs=total_backs,
        missing_images=missing,
        size_bytes=output_path.stat().st_size,
    )


def _build_readme(total_cards: int, total_dfc: int) -> str:
    return (
        "Export de MPC Forge\n"
        "===================\n\n"
        f"- {total_cards} carta{'s' if total_cards != 1 else ''} única{'s' if total_cards != 1 else ''} (una imagen por carta).\n"
        f"- {total_dfc} carta{'s' if total_dfc != 1 else ''} DFC con reverso incluido.\n"
        "- Cada imagen se llama como la carta oficial (según Scryfall).\n"
        "- Los reversos de DFC están nombrados por la cara-B (\"Insectile Aberration.png\"),\n"
        "  no por la cara-A. Si tu herramienta espera '<frente>__back.<ext>' renombra a mano.\n\n"
        "decklist.txt: lista serializada del mazo. Se puede importar en Moxfield,\n"
        "Archidekt, MTGGoldfish y prácticamente cualquier editor que acepte texto plano.\n\n"
        "Las cantidades del mazo NO están reflejadas en los ficheros de imagen\n"
        "(una imagen por carta única). Cuenta las copias desde decklist.txt.\n"
    )
