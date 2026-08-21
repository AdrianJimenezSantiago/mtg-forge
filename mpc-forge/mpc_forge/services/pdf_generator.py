"""Generador de PDF imprimible con las cartas del mazo.

Diseño estilo MTG Print: 9 cartas por página A4 (3×3) a **tamaño real MTG**
(63 × 88 mm), listas para recortar con guillotina o tijeras. Calidad máxima:
las imágenes van sin recomprimir (ReportLab acepta PNG/JPG directos).

- Tamaño página: A4 por defecto (opcional Letter).
- Cada carta: 63 × 88 mm — el estándar de MTG. Coincide con lo que MakePlayingCards
  imprime, por lo que sirven para pruebas 1:1.
- Marcas de corte opcionales entre cartas.
- Espacio entre cartas configurable (0 mm por defecto para maximizar aprovechamiento
  y hacer cortes rectos con guillotina).
- DFC: por defecto se incluye solo el frente. Con `include_backs=True` se añaden
  todos los reversos al final del PDF.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from mpc_forge.services.xml_generator import DeckCardResolved

log = logging.getLogger(__name__)

# Tamaño exacto de una carta MTG en mm (área visible; MPC añade bleed a 66.75×91.75).
CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0

PageSize = Literal["a4", "letter"]


@dataclass
class PDFOptions:
    page_size: PageSize = "a4"
    include_backs: bool = False
    cut_marks: bool = True
    gap_mm: float = 0.0                # separación entre cartas
    margin_mm: float | None = None     # centrado automático si es None


@dataclass
class PDFBuildResult:
    pdf_path: Path
    total_pages: int
    total_slots: int


def _expand_slots(cards: list[DeckCardResolved], include_backs: bool) -> list[dict]:
    """Genera un slot por cada copia. Front primero (respetando cantidad), luego
    reversos DFC al final si include_backs."""
    fronts: list[dict] = []
    backs: list[dict] = []
    for c in cards:
        for _ in range(c.quantity):
            fronts.append({"name": c.name, "path": c.front_path, "face": "front"})
            if c.back_path:
                backs.append({"name": (c.back_name or c.name), "path": c.back_path, "face": "back"})
    return fronts + (backs if include_backs else [])


def build_pdf(
    cards: list[DeckCardResolved],
    output_path: Path,
    options: PDFOptions | None = None,
) -> PDFBuildResult:
    opts = options or PDFOptions()
    page = A4 if opts.page_size == "a4" else LETTER
    page_w, page_h = page  # en puntos (1 punto = 1/72 pulgada)

    card_w = CARD_WIDTH_MM * mm
    card_h = CARD_HEIGHT_MM * mm
    gap = opts.gap_mm * mm

    cols, rows = 3, 3
    grid_w = cols * card_w + (cols - 1) * gap
    grid_h = rows * card_h + (rows - 1) * gap

    # Centrado horizontal y vertical si margin no está explícito.
    if opts.margin_mm is None:
        offset_x = (page_w - grid_w) / 2
        offset_y = (page_h - grid_h) / 2
    else:
        offset_x = opts.margin_mm * mm
        offset_y = opts.margin_mm * mm

    slots = _expand_slots(cards, opts.include_backs)
    if not slots:
        raise ValueError("No hay cartas resueltas para generar el PDF")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=page)
    c.setTitle("MPC Forge — Print sheet")
    c.setAuthor("MPC Forge")

    # Metadata para lectores PDF
    c.setSubject(f"{len(slots)} card proxies, 3×3 per page, {CARD_WIDTH_MM}×{CARD_HEIGHT_MM} mm")

    per_page = cols * rows
    total_pages = 0

    for page_idx, start in enumerate(range(0, len(slots), per_page)):
        chunk = slots[start:start + per_page]
        total_pages += 1

        for i, slot in enumerate(chunk):
            row = i // cols
            col = i % cols
            # Origen bottom-left en PDF; queremos que la fila 0 sea la de arriba.
            x = offset_x + col * (card_w + gap)
            y = offset_y + (rows - 1 - row) * (card_h + gap)

            try:
                # ReportLab acepta rutas directamente, sin recomprimir cuando
                # es JPEG. Para PNG lo pasa como bitmap sin pérdida.
                c.drawImage(
                    str(slot["path"]),
                    x, y,
                    width=card_w,
                    height=card_h,
                    preserveAspectRatio=False,   # queremos tamaño exacto MTG
                    mask="auto",
                )
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo pintar %s: %s", slot["path"], e)
                # Placeholder gris con el nombre para no dejar hueco:
                c.setFillGray(0.15)
                c.rect(x, y, card_w, card_h, stroke=1, fill=1)
                c.setFillGray(0.85)
                c.setFont("Helvetica", 8)
                c.drawCentredString(x + card_w / 2, y + card_h / 2, slot["name"])

        if opts.cut_marks:
            _draw_cut_marks(c, offset_x, offset_y, card_w, card_h, gap, cols, rows)

        # Pie con nº de página / total slots
        c.setFillGray(0.55)
        c.setFont("Helvetica", 7)
        footer = f"MPC Forge  ·  página {page_idx + 1}  ·  {len(slots)} slots totales"
        c.drawRightString(page_w - 10 * mm, 6 * mm, footer)

        c.showPage()

    c.save()
    return PDFBuildResult(
        pdf_path=output_path,
        total_pages=total_pages,
        total_slots=len(slots),
    )


def _draw_cut_marks(
    c: canvas.Canvas,
    offset_x: float, offset_y: float,
    card_w: float, card_h: float, gap: float,
    cols: int, rows: int,
) -> None:
    """Cruces finas en cada esquina de cada carta, extendiéndose hacia afuera
    del área impresa para poder alinear la guillotina."""
    mark_len = 3 * mm
    c.setStrokeGray(0.4)
    c.setLineWidth(0.3)

    for row in range(rows):
        for col in range(cols):
            x0 = offset_x + col * (card_w + gap)
            y0 = offset_y + (rows - 1 - row) * (card_h + gap)
            x1 = x0 + card_w
            y1 = y0 + card_h

            # Cuatro esquinas, dos líneas cortas cada una (horizontal + vertical)
            for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                # Solo dibujamos hacia AFUERA de la carta
                dx_out = -mark_len if cx == x0 else mark_len
                dy_out = -mark_len if cy == y0 else mark_len
                c.line(cx, cy, cx + dx_out, cy)
                c.line(cx, cy, cx, cy + dy_out)
