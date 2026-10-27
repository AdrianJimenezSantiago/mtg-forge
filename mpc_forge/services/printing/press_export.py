from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from reportlab import rl_config
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfdoc import PDFArray, PDFDictionary, PDFName, PDFString
from reportlab.pdfgen import canvas

from mpc_forge import __version__
from mpc_forge.services.printing.press_color import CmykConverter
from mpc_forge.services.printing.press_image import PressImage, prepare_press_image
from mpc_forge.services.printing.print_shops import PrintShopSpec
from mpc_forge.services.printing.xml_generator import DeckCardResolved

log = logging.getLogger(__name__)

rl_config.useA85 = 0

Face = Literal["front", "back"]

_MAX_WORKERS = max(1, min(8, (os.cpu_count() or 2)))


class _ImageOnlyCanvas(canvas.Canvas):  # type: ignore[misc]
    """Canvas para páginas que solo llevan una imagen.

    ReportLab abre cada página con "BT /F1 12 Tf 14.4 TL ET", y esa línea añade
    Helvetica sin incrustar al PDF aunque no haya texto. Las imprentas exigen
    las fuentes incrustadas, así que el preámbulo se queda en la matriz inicial.
    """

    def _make_preamble(self) -> None:
        self._preamble = "1 0 0 1 0 0 cm"


class PressExportError(ValueError):
    """El mazo no se puede mandar a la imprenta tal como está."""

    def __init__(self, code: str, message: str, cards: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.cards = cards or []


@dataclass(frozen=True)
class PressSlot:
    card_name: str
    face_name: str
    face: Face
    path: Path
    source: Literal["card", "dfc", "cardback"]


@dataclass(frozen=True)
class _StagedImage:
    """Una imagen ya preparada, en disco: en memoria solo quedan sus datos."""

    file: Path
    size: int
    generated_bleed: bool
    source_ppi: int
    warnings: list[str]


def _stage(image: PressImage, file: Path) -> _StagedImage:
    file.write_bytes(image.jpeg)
    return _StagedImage(
        file=file,
        size=len(image.jpeg),
        generated_bleed=image.generated_bleed,
        source_ppi=image.source_ppi,
        warnings=image.warnings,
    )


@dataclass
class PressWarning:
    card_name: str
    face: Face
    code: str


@dataclass
class PressBuildResult:
    pdf_path: Path
    total_cards: int
    total_pages: int
    unique_images: int
    dfc_backs: int
    cardback_backs: int
    generated_bleed_images: int
    pdf_size_bytes: int
    largest_image_bytes: int
    min_source_ppi: int
    warnings: list[PressWarning] = field(default_factory=list)


def build_press_slots(
    cards: list[DeckCardResolved],
    cardback_path: Path | None,
) -> list[tuple[PressSlot, PressSlot]]:
    """Una pareja (anverso, reverso) por copia, en el orden del mazo.

    Las cartas de dos caras llevan su otra cara detrás; el resto, el dorso del
    mazo. Sin dorso no hay pedido posible: la imprenta imprime a doble cara.
    """
    needs_cardback = [c.name for c in cards if c.back_path is None]
    if needs_cardback and (cardback_path is None or not cardback_path.exists()):
        raise PressExportError(
            "missing_cardback",
            "El mazo no tiene dorso: elige uno en PDF Studio antes de exportar.",
            sorted(set(needs_cardback)),
        )

    pairs: list[tuple[PressSlot, PressSlot]] = []
    for c in cards:
        front = PressSlot(c.name, c.name.split(" // ", 1)[0], "front", Path(c.front_path), "card")
        if c.back_path is not None:
            back = PressSlot(c.name, c.back_name or c.name, "back", Path(c.back_path), "dfc")
        else:
            assert cardback_path is not None
            back = PressSlot(c.name, "Cardback", "back", cardback_path, "cardback")
        pairs.extend((front, back) for _ in range(c.quantity))
    return pairs


def build_press_pdf(
    pairs: list[tuple[PressSlot, PressSlot]],
    output_path: Path,
    spec: PrintShopSpec,
    *,
    title: str,
    on_image: Callable[[str], None] | None = None,
) -> PressBuildResult:
    if not pairs:
        raise PressExportError("empty", "No hay cartas que exportar.")

    converter = CmykConverter(spec.icc_path)
    unique: dict[Path, PressSlot] = {}
    for front, back in pairs:
        unique.setdefault(front.path, front)
        unique.setdefault(back.path, back)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".press-", dir=output_path.parent) as workdir:
        prepared = _prepare_all(unique, spec, converter, on_image, Path(workdir))
        _write_pdf(pairs, prepared, output_path, spec, title)

    warnings: list[PressWarning] = []
    for path, slot in unique.items():
        warnings.extend(
            PressWarning(slot.face_name, slot.face, code) for code in prepared[path].warnings
        )

    images = list(prepared.values())
    result = PressBuildResult(
        pdf_path=output_path,
        total_cards=len(pairs),
        total_pages=2 * len(pairs),
        unique_images=len(prepared),
        dfc_backs=sum(1 for _, b in pairs if b.source == "dfc"),
        cardback_backs=sum(1 for _, b in pairs if b.source == "cardback"),
        generated_bleed_images=sum(1 for i in images if i.generated_bleed),
        pdf_size_bytes=output_path.stat().st_size,
        largest_image_bytes=max(i.size for i in images),
        min_source_ppi=min(i.source_ppi for i in images),
        warnings=warnings,
    )
    preflight(result, spec)
    log.info(
        "PDF de imprenta %s: %d cartas, %d páginas, %d imágenes únicas, %.1f MB",
        spec.key,
        result.total_cards,
        result.total_pages,
        result.unique_images,
        result.pdf_size_bytes / 1e6,
    )
    return result


def _write_pdf(
    pairs: list[tuple[PressSlot, PressSlot]],
    prepared: dict[Path, _StagedImage],
    output_path: Path,
    spec: PrintShopSpec,
    title: str,
) -> None:
    page_w, page_h = spec.page_w_mm * mm, spec.page_h_mm * mm
    bleed = spec.bleed_mm * mm
    c = _ImageOnlyCanvas(str(output_path), pagesize=(page_w, page_h), pageCompression=1)
    c.setTitle(title)
    c.setAuthor("MPC Forge")
    c.setCreator(f"MPC Forge {__version__}")
    c.setSubject(
        f"{spec.shop_name} · {spec.product_name} · {len(pairs)} cartas · "
        f"anverso y reverso · {spec.output_condition}"
    )
    _add_output_intent(c, spec)

    for front, back in pairs:
        for slot in (front, back):
            c.setBleedBox((0, 0, page_w, page_h))
            c.setTrimBox((bleed, bleed, page_w - bleed, page_h - bleed))
            c.drawImage(str(prepared[slot.path].file), 0, 0, width=page_w, height=page_h)
            c.showPage()
    c.save()


def preflight(result: PressBuildResult, spec: PrintShopSpec) -> None:
    """Comprueba lo que la imprenta rechazaría. Un fallo aquí es un error del
    programa, no del mazo: lo demás se ha validado antes de pintar."""
    if result.total_pages != 2 * result.total_cards:
        raise PressExportError(
            "page_count", f"{result.total_pages} páginas para {result.total_cards} cartas"
        )
    budget = spec.max_bytes_per_page * result.total_pages
    if result.pdf_size_bytes > budget:
        raise PressExportError(
            "file_too_large",
            f"El PDF pesa {result.pdf_size_bytes / 1e6:.1f} MB y el máximo para "
            f"{result.total_pages} páginas es {budget / 1e6:.0f} MB",
        )
    if result.largest_image_bytes > spec.max_bytes_per_page:
        raise PressExportError("page_too_large", "Una página supera el límite de la imprenta")


def build_press_package(pdf_path: Path, zip_path: Path, spec: PrintShopSpec) -> int:
    """ZIP listo para WeTransfer: una carpeta por componente y el PDF con la
    medida en el nombre, como pide la guía."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        zf.write(pdf_path, arcname=spec.package_entry)
    return zip_path.stat().st_size


def _prepare_all(
    unique: dict[Path, PressSlot],
    spec: PrintShopSpec,
    converter: CmykConverter,
    on_image: Callable[[str], None] | None,
    workdir: Path,
) -> dict[Path, _StagedImage]:
    prepared: dict[Path, _StagedImage] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(prepare_press_image, path, spec, converter): path for path in unique}
        try:
            for future in as_completed(futures):
                path = futures[future]
                prepared[path] = _stage(future.result(), workdir / f"{len(prepared):04d}.jpg")
                if on_image is not None:
                    on_image(unique[path].face_name)
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise
    return prepared


def _add_output_intent(c: canvas.Canvas, spec: PrintShopSpec) -> None:
    intent = PDFDictionary(
        {
            "Type": PDFName("OutputIntent"),
            "S": PDFName("GTS_PDFX"),
            "OutputConditionIdentifier": PDFString(spec.output_condition_identifier),
            "OutputCondition": PDFString(spec.output_condition),
            "RegistryName": PDFString(spec.output_registry),
            "Info": PDFString(spec.output_condition),
        }
    )
    catalog = c._doc.Catalog
    catalog.__NoDefault__ = [*catalog.__NoDefault__, "OutputIntents"]
    catalog.OutputIntents = PDFArray([intent])
