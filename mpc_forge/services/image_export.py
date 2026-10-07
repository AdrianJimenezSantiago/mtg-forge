from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from mpc_forge.services.xml_generator import DeckCardResolved

log = logging.getLogger(__name__)

_FORBIDDEN = set('/\\?*|"<>:')
_CONTROL = {chr(i) for i in range(0, 32)}


def _safe_filename(name: str, max_len: int = 120) -> str:
    cleaned = "".join("_" if (c in _FORBIDDEN or c in _CONTROL) else c for c in name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "unnamed"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(". ")
    return cleaned


def _split_dfc(full_name: str) -> tuple[str, str | None]:
    if " // " in full_name:
        front, back = full_name.split(" // ", 1)
        return front.strip(), back.strip()
    return full_name.strip(), None


@dataclass
class ImageExportResult:
    zip_path: Path
    total_files: int
    total_unique_cards: int
    total_dfc_backs: int
    included_cardback: bool
    missing_images: int
    size_bytes: int


def build_images_zip(
    cards: list[DeckCardResolved],
    output_path: Path,
    decklist_text: str,
    cardback_path: Path | None = None,
) -> ImageExportResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen_files: dict[str, str] = {}
    missing = 0
    total_backs = 0
    included_cardback = False

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_STORED) as zf:
        for c in cards:
            front_face, back_face = _split_dfc(c.name)
            ext = Path(str(c.front_path)).suffix or ".png"
            arc_front = f"{_safe_filename(front_face)}{ext}"

            src = Path(str(c.front_path))
            if arc_front in seen_files and seen_files[arc_front] != str(src):
                arc_front = f"{_safe_filename(front_face)}__{c.scryfall_id[:8]}{ext}"

            if arc_front not in seen_files:
                try:
                    zf.write(src, arcname=arc_front)
                    seen_files[arc_front] = str(src)
                except FileNotFoundError:
                    log.warning("Imagen de frente no encontrada: %s", src)
                    missing += 1

            if c.back_path:
                back_name = c.back_name or back_face or f"{front_face}__back"
                back_ext = Path(str(c.back_path)).suffix or ".png"
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

        if cardback_path is not None and cardback_path.exists():
            cb_ext = cardback_path.suffix or ".png"
            cb_arc = f"_cardback{cb_ext}"
            try:
                zf.write(cardback_path, arcname=cb_arc)
                seen_files[cb_arc] = str(cardback_path)
                included_cardback = True
            except FileNotFoundError:
                log.warning("Cardback no encontrado: %s", cardback_path)

        zf.writestr("decklist.txt", decklist_text, compress_type=zipfile.ZIP_DEFLATED)

        readme = _build_readme(
            len(cards),
            len([c for c in cards if c.back_path]),
            included_cardback,
        )
        zf.writestr("README.txt", readme, compress_type=zipfile.ZIP_DEFLATED)

    total_files = len(seen_files) + 2
    return ImageExportResult(
        zip_path=output_path,
        total_files=total_files,
        total_unique_cards=len(cards),
        total_dfc_backs=total_backs,
        included_cardback=included_cardback,
        missing_images=missing,
        size_bytes=output_path.stat().st_size,
    )


def _build_readme(total_cards: int, total_dfc: int, included_cardback: bool) -> str:
    lines = [
        "Export de MPC Forge",
        "===================",
        "",
        f"- {total_cards} carta{'s' if total_cards != 1 else ''} única{'s' if total_cards != 1 else ''} (una imagen por carta).",
        f"- {total_dfc} carta{'s' if total_dfc != 1 else ''} DFC con reverso incluido.",
        "- Cada imagen se llama como la carta oficial (según Scryfall).",
        '- Los reversos de DFC están nombrados por la cara-B ("Insectile Aberration.png"),',
        "  no por la cara-A. Si tu herramienta espera '<frente>__back.<ext>' renombra a mano.",
    ]
    if included_cardback:
        lines += [
            "- _cardback.<ext>: reverso genérico del mazo. Úsalo como back de todas las",
            "  cartas que no sean DFC/MDFC/meld (esas ya llevan su propio reverso).",
        ]
    lines += [
        "",
        "decklist.txt: lista serializada del mazo. Se puede importar en Moxfield,",
        "Archidekt, MTGGoldfish y prácticamente cualquier editor que acepte texto plano.",
        "",
        "Las cantidades del mazo NO están reflejadas en los ficheros de imagen",
        "(una imagen por carta única). Cuenta las copias desde decklist.txt.",
        "",
    ]
    return "\n".join(lines)
