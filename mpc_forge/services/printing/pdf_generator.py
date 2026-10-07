from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A3, A4, LETTER, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from mpc_forge.services.printing.xml_generator import DeckCardResolved, default_cardback_path

log = logging.getLogger(__name__)

CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0


class _ImageReaderCache:
    def __init__(self) -> None:
        self._readers: dict[str, ImageReader] = {}
        self.hits = 0
        self.misses = 0

    def get(self, path: str) -> ImageReader:
        reader = self._readers.get(path)
        if reader is None:
            reader = ImageReader(path)
            self._readers[path] = reader
            self.misses += 1
        else:
            self.hits += 1
        return reader

    def summary(self) -> str:
        total = self.hits + self.misses
        if not total:
            return "sin imágenes"
        return (
            f"{self.misses} imágenes únicas, {self.hits} reutilizaciones "
            f"({100 * self.hits // total}% de ahorro)"
        )


PageSize = Literal["a4", "letter", "a3"]
Orientation = Literal["portrait", "landscape"]
CardGuidesStyle = Literal["corners", "full"]
CardGuidesShape = Literal["square", "round"]
GuidesPattern = Literal["solid", "dashed", "dotted"]
GuidesPlacement = Literal["outside", "middle", "inside"]
PageGuides = Literal["none", "full_lines", "corners_only"]
BacksLayout = Literal["append", "duplex"]
BacksContent = Literal["dfc_only", "all_cards"]

_PAGE_SIZES = {
    "a4": A4,
    "letter": LETTER,
    "a3": A3,
}


@dataclass
class PDFOptions:
    page_size: PageSize = "a4"
    orientation: Orientation = "portrait"

    cols: int = 3
    rows: int = 3

    gap_x_mm: float = 0.0
    gap_y_mm: float = 0.0

    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    back_offset_x_mm: float = 0.0
    back_offset_y_mm: float = 0.0

    bleed_enabled: bool = False
    bleed_mm: float = 0.0

    card_guides_enabled: bool = True
    card_guides_style: CardGuidesStyle = "corners"
    card_guides_shape: CardGuidesShape = "square"
    card_guides_pattern: GuidesPattern = "solid"
    card_guides_placement: GuidesPlacement = "outside"
    card_guides_length_mm: float = 4.0
    card_guides_color: str = "#606060"
    card_guides_width_pt: float = 0.4

    page_guides: PageGuides = "none"

    hide_card_guides_front: bool = False
    hide_card_guides_back: bool = False
    hide_page_guides_front: bool = False
    hide_page_guides_back: bool = False

    reg_marks_enabled: bool = False
    reg_marks_inset_mm: float = 10.0
    reg_marks_size_mm: float = 5.0

    include_backs: bool = False
    backs_layout: BacksLayout = "append"
    backs_content: BacksContent = "all_cards"
    backs_compact_fill: bool = True

    page_range: str = ""

    show_footer: bool = True


@dataclass
class PDFBuildResult:
    pdf_path: Path
    total_pages: int
    total_slots: int
    cols: int
    rows: int


@dataclass
class Geometry:
    page_w_mm: float
    page_h_mm: float
    cols: int
    rows: int
    card_w_mm: float
    card_h_mm: float
    slot_w_mm: float
    slot_h_mm: float
    gap_x_mm: float
    gap_y_mm: float
    grid_w_mm: float
    grid_h_mm: float
    origin_x_mm: float
    origin_y_mm: float
    bleed_mm: float


def _page_size_pt(opts: PDFOptions) -> tuple[float, float]:
    size = _PAGE_SIZES.get(opts.page_size, A4)
    if opts.orientation == "landscape":
        size = landscape(size)
    width, height = size
    return float(width), float(height)


def compute_geometry(opts: PDFOptions, page_kind: str = "front") -> Geometry:
    page_w_pt, page_h_pt = _page_size_pt(opts)
    page_w_mm = page_w_pt / mm
    page_h_mm = page_h_pt / mm

    bleed = opts.bleed_mm if opts.bleed_enabled else 0.0
    slot_w = CARD_WIDTH_MM + 2 * bleed
    slot_h = CARD_HEIGHT_MM + 2 * bleed

    cols = (
        opts.cols
        if opts.cols > 0
        else max(1, int((page_w_mm + opts.gap_x_mm) // (slot_w + opts.gap_x_mm)))
    )
    rows = (
        opts.rows
        if opts.rows > 0
        else max(1, int((page_h_mm + opts.gap_y_mm) // (slot_h + opts.gap_y_mm)))
    )

    grid_w = cols * slot_w + (cols - 1) * opts.gap_x_mm
    grid_h = rows * slot_h + (rows - 1) * opts.gap_y_mm

    ox = opts.offset_x_mm + (opts.back_offset_x_mm if page_kind == "back" else 0.0)
    oy = opts.offset_y_mm + (opts.back_offset_y_mm if page_kind == "back" else 0.0)
    origin_x = (page_w_mm - grid_w) / 2 + ox
    origin_y = (page_h_mm - grid_h) / 2 + oy

    return Geometry(
        page_w_mm=page_w_mm,
        page_h_mm=page_h_mm,
        cols=cols,
        rows=rows,
        card_w_mm=CARD_WIDTH_MM,
        card_h_mm=CARD_HEIGHT_MM,
        slot_w_mm=slot_w,
        slot_h_mm=slot_h,
        gap_x_mm=opts.gap_x_mm,
        gap_y_mm=opts.gap_y_mm,
        grid_w_mm=grid_w,
        grid_h_mm=grid_h,
        origin_x_mm=origin_x,
        origin_y_mm=origin_y,
        bleed_mm=bleed,
    )


def _slot_position_mm(g: Geometry, col: int, row: int) -> tuple[float, float]:
    x = g.origin_x_mm + col * (g.slot_w_mm + g.gap_x_mm)
    y = g.origin_y_mm + (g.rows - 1 - row) * (g.slot_h_mm + g.gap_y_mm)
    return x, y


def _expand_slots(
    cards: list[DeckCardResolved],
    cardback_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
    fronts: list[dict[str, Any]] = []
    backs: list[dict[str, Any] | None] = []
    for c in cards:
        for _ in range(c.quantity):
            fronts.append({"name": c.name, "path": str(c.front_path), "face": "front"})
            if c.back_path:
                backs.append(
                    {
                        "name": c.back_name or c.name,
                        "path": str(c.back_path),
                        "face": "back",
                    }
                )
            elif cardback_path is not None:
                backs.append(
                    {
                        "name": "Cardback",
                        "path": str(cardback_path),
                        "face": "back",
                    }
                )
            else:
                backs.append(None)
    return fronts, backs


def _parse_page_range(spec: str, total: int) -> list[int]:
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    result: set[int] = set()
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            for n in range(min(lo, hi), max(lo, hi) + 1):
                if 1 <= n <= total:
                    result.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                result.add(n)
    return sorted(result) if result else list(range(1, total + 1))


def build_pdf(
    cards: list[DeckCardResolved],
    output_path: Path,
    options: PDFOptions | None = None,
    cardback_path_override: Path | None = None,
) -> PDFBuildResult:
    opts = options or PDFOptions()

    cardback: Path | None = None
    if opts.include_backs and opts.backs_content == "all_cards":
        cardback = cardback_path_override or default_cardback_path()
        if cardback is None:
            log.warning(
                "backs_content='all_cards' pero no hay cardback disponible — "
                "los slots no-DFC de las páginas de reversos quedarán vacíos."
            )

    fronts, backs = _expand_slots(cards, cardback)
    if not fronts:
        raise ValueError("No hay cartas resueltas para generar el PDF")

    g_front = compute_geometry(opts, page_kind="front")
    per_page = g_front.cols * g_front.rows

    pages: list[tuple[str, int, list[dict[str, Any] | None]]] = []

    front_chunks: list[list[dict[str, Any] | None]] = [
        list(fronts[i : i + per_page]) for i in range(0, len(fronts), per_page)
    ]

    if opts.include_backs and opts.backs_layout == "duplex":
        for pidx, chunk in enumerate(front_chunks):
            pages.append(("front", pidx, chunk))
            start = pidx * per_page
            back_chunk = backs[start : start + per_page]
            if any(b is not None for b in back_chunk):
                pages.append(("back", pidx, list(back_chunk)))
    elif opts.include_backs and opts.backs_layout == "append":
        real_backs: list[dict[str, Any]] = [b for b in backs if b is not None]
        back_idx = 0
        if opts.backs_compact_fill and front_chunks:
            last = front_chunks[-1]
            free = per_page - len(last)
            if free > 0 and real_backs:
                take = min(free, len(real_backs))
                last.extend(real_backs[:take])
                back_idx = take
        for pidx, chunk in enumerate(front_chunks):
            pages.append(("front", pidx, chunk))
        remaining = real_backs[back_idx:]
        for pidx, i in enumerate(range(0, len(remaining), per_page)):
            pages.append(("back", pidx, list(remaining[i : i + per_page])))
    else:
        for pidx, chunk in enumerate(front_chunks):
            pages.append(("front", pidx, chunk))

    selected = _parse_page_range(opts.page_range, len(pages))
    pages_to_render = [pages[i - 1] for i in selected]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    page_w_pt, page_h_pt = _page_size_pt(opts)
    c = canvas.Canvas(str(output_path), pagesize=(page_w_pt, page_h_pt))
    c.setTitle("MPC Forge — Print sheet")
    c.setAuthor("MPC Forge")
    c.setSubject(
        f"{len(fronts)} card fronts, {g_front.cols}×{g_front.rows} per page, "
        f"{CARD_WIDTH_MM}×{CARD_HEIGHT_MM} mm"
    )

    image_cache = _ImageReaderCache()

    total_pages_rendered = 0
    for page_idx, (kind, chunk_idx, chunk) in enumerate(pages_to_render):
        g = g_front if kind == "front" else compute_geometry(opts, page_kind="back")
        _render_page(c, g, opts, kind, chunk, image_cache)
        if opts.show_footer:
            _draw_footer(c, g, page_idx + 1, len(pages_to_render), kind, chunk_idx)
        c.showPage()
        total_pages_rendered += 1

    c.save()
    log.info(
        "PDF generado: %d páginas, %d slots. Imágenes: %s",
        total_pages_rendered,
        len(fronts),
        image_cache.summary(),
    )
    return PDFBuildResult(
        pdf_path=output_path,
        total_pages=total_pages_rendered,
        total_slots=len(fronts),
        cols=g_front.cols,
        rows=g_front.rows,
    )


def _render_page(
    c: canvas.Canvas,
    g: Geometry,
    opts: PDFOptions,
    kind: str,
    chunk: list[dict[str, Any] | None],
    image_cache: _ImageReaderCache | None = None,
) -> None:
    is_back_duplex = kind == "back" and opts.backs_layout == "duplex"

    img_x_off = g.bleed_mm
    img_y_off = g.bleed_mm
    img_w = g.slot_w_mm - 2 * g.bleed_mm
    img_h = g.slot_h_mm - 2 * g.bleed_mm
    for i, slot in enumerate(chunk):
        if slot is None:
            continue
        row = i // g.cols
        col = i % g.cols
        if is_back_duplex:
            col = g.cols - 1 - col
        x_mm, y_mm = _slot_position_mm(g, col, row)
        try:
            c.drawImage(
                image_cache.get(slot["path"]) if image_cache else slot["path"],
                (x_mm + img_x_off) * mm,
                (y_mm + img_y_off) * mm,
                width=img_w * mm,
                height=img_h * mm,
                preserveAspectRatio=False,
                mask="auto",
            )
        except Exception as e:
            log.error("No se pudo pintar %s: %s", slot["path"], e)
            _placeholder(c, x_mm + img_x_off, y_mm + img_y_off, img_w, img_h, slot["name"])

    _draw_card_guides(c, g, opts, kind)

    _draw_page_guides(c, g, opts, kind)

    if opts.reg_marks_enabled:
        _draw_registration_marks(c, g, opts)


def _placeholder(
    c: canvas.Canvas,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    name: str,
) -> None:
    c.saveState()
    c.setFillGray(0.15)
    c.rect(x_mm * mm, y_mm * mm, w_mm * mm, h_mm * mm, stroke=1, fill=1)
    c.setFillGray(0.85)
    c.setFont("Helvetica", 8)
    c.drawCentredString(
        (x_mm + w_mm / 2) * mm,
        (y_mm + h_mm / 2) * mm,
        name[:40],
    )
    c.restoreState()


def _apply_stroke_style(
    c: canvas.Canvas,
    color_hex: str,
    width_pt: float,
    pattern: str,
) -> None:
    c.setStrokeColor(HexColor(color_hex))
    c.setLineWidth(width_pt)
    if pattern == "dashed":
        c.setDash(2, 2)
        c.setLineCap(0)
    elif pattern == "dotted":
        c.setDash(0.1, 1.5)
        c.setLineCap(1)
    else:
        c.setDash()
        c.setLineCap(0)


def _draw_card_guides(
    c: canvas.Canvas,
    g: Geometry,
    opts: PDFOptions,
    kind: str,
) -> None:
    if not opts.card_guides_enabled:
        return
    if kind == "front" and opts.hide_card_guides_front:
        return
    if kind == "back" and opts.hide_card_guides_back:
        return

    c.saveState()
    _apply_stroke_style(
        c,
        opts.card_guides_color,
        opts.card_guides_width_pt,
        opts.card_guides_pattern,
    )
    if opts.card_guides_style == "full":
        _draw_card_full_rects(c, g, opts)
    else:
        _draw_card_corner_marks(c, g, opts)
    c.restoreState()


def _draw_card_full_rects(c: canvas.Canvas, g: Geometry, opts: PDFOptions) -> None:
    r_mm = 3.0
    for col in range(g.cols):
        for row in range(g.rows):
            x_slot, y_slot = _slot_position_mm(g, col, row)
            x = x_slot + g.bleed_mm
            y = y_slot + g.bleed_mm
            w = g.slot_w_mm - 2 * g.bleed_mm
            h = g.slot_h_mm - 2 * g.bleed_mm
            if opts.card_guides_shape == "round":
                c.roundRect(x * mm, y * mm, w * mm, h * mm, r_mm * mm, stroke=1, fill=0)
            else:
                c.rect(x * mm, y * mm, w * mm, h * mm, stroke=1, fill=0)


def _draw_card_corner_marks(c: canvas.Canvas, g: Geometry, opts: PDFOptions) -> None:
    L = opts.card_guides_length_mm
    for col in range(g.cols):
        for row in range(g.rows):
            x_slot, y_slot = _slot_position_mm(g, col, row)
            xL = x_slot + g.bleed_mm
            xR = x_slot + g.slot_w_mm - g.bleed_mm
            yB = y_slot + g.bleed_mm
            yT = y_slot + g.slot_h_mm - g.bleed_mm

            for cx, cy, dx, dy in [
                (xL, yB, -1, -1),
                (xR, yB, +1, -1),
                (xL, yT, -1, +1),
                (xR, yT, +1, +1),
            ]:
                _draw_corner(c, cx, cy, dx, dy, L, opts)


def _draw_corner(
    c: canvas.Canvas,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
    L: float,
    opts: PDFOptions,
) -> None:
    placement = opts.card_guides_placement

    if placement == "outside":
        h_start, h_end = (cx, cx + dx * L)
        v_start, v_end = (cy, cy + dy * L)
    elif placement == "inside":
        h_start, h_end = (cx, cx - dx * L)
        v_start, v_end = (cy, cy - dy * L)
    else:
        half = L / 2
        h_start, h_end = (cx - dx * half, cx + dx * half)
        v_start, v_end = (cy - dy * half, cy + dy * half)

    if opts.card_guides_shape == "square":
        c.line(h_start * mm, cy * mm, h_end * mm, cy * mm)
        c.line(cx * mm, v_start * mm, cx * mm, v_end * mm)
        return

    K = 0.5522847498
    p = c.beginPath()
    p.moveTo(h_end * mm, cy * mm)
    if placement == "middle":
        cp1_x = h_end - dx * (L / 2) * K
        cp1_y = cy + dy * (L / 2) * K
        cp2_x = cx + dx * (L / 2) * K
        cp2_y = v_end - dy * (L / 2) * K
    else:
        cp1_x = h_end
        cp1_y = cy + (v_end - cy) * K
        cp2_x = cx + (h_end - cx) * K
        cp2_y = v_end
    p.curveTo(cp1_x * mm, cp1_y * mm, cp2_x * mm, cp2_y * mm, cx * mm, v_end * mm)
    c.drawPath(p, stroke=1, fill=0)


def _draw_page_guides(
    c: canvas.Canvas,
    g: Geometry,
    opts: PDFOptions,
    kind: str,
) -> None:
    if opts.page_guides == "none":
        return
    if kind == "front" and opts.hide_page_guides_front:
        return
    if kind == "back" and opts.hide_page_guides_back:
        return

    c.saveState()
    _apply_stroke_style(
        c,
        opts.card_guides_color,
        opts.card_guides_width_pt,
        opts.card_guides_pattern,
    )
    if opts.page_guides == "full_lines":
        _draw_page_full_lines(c, g)
    else:
        _draw_page_corner_lines(c, g, opts)
    c.restoreState()


def _guide_line_positions(g: Geometry) -> tuple[list[float], list[float]]:
    verticals: set[float] = set()
    horizontals: set[float] = set()
    for col in range(g.cols):
        for row in range(g.rows):
            x_slot, y_slot = _slot_position_mm(g, col, row)
            xL = x_slot + g.bleed_mm
            xR = x_slot + g.slot_w_mm - g.bleed_mm
            yB = y_slot + g.bleed_mm
            yT = y_slot + g.slot_h_mm - g.bleed_mm
            verticals.add(round(xL, 3))
            verticals.add(round(xR, 3))
            horizontals.add(round(yB, 3))
            horizontals.add(round(yT, 3))
    return sorted(verticals), sorted(horizontals)


def _draw_page_full_lines(c: canvas.Canvas, g: Geometry) -> None:
    verticals, horizontals = _guide_line_positions(g)
    for x_mm in verticals:
        c.line(x_mm * mm, 0, x_mm * mm, g.page_h_mm * mm)
    for y_mm in horizontals:
        c.line(0, y_mm * mm, g.page_w_mm * mm, y_mm * mm)


def _draw_page_corner_lines(c: canvas.Canvas, g: Geometry, opts: PDFOptions) -> None:
    verticals, horizontals = _guide_line_positions(g)
    L = opts.card_guides_length_mm

    grid_x1 = g.origin_x_mm
    grid_x2 = g.origin_x_mm + g.grid_w_mm
    grid_y1 = g.origin_y_mm
    grid_y2 = g.origin_y_mm + g.grid_h_mm

    for x_mm in verticals:
        c.line(x_mm * mm, max(0, grid_y1 - L) * mm, x_mm * mm, grid_y1 * mm)
        c.line(x_mm * mm, grid_y2 * mm, x_mm * mm, min(g.page_h_mm, grid_y2 + L) * mm)
    for y_mm in horizontals:
        c.line(max(0, grid_x1 - L) * mm, y_mm * mm, grid_x1 * mm, y_mm * mm)
        c.line(grid_x2 * mm, y_mm * mm, min(g.page_w_mm, grid_x2 + L) * mm, y_mm * mm)


def _draw_registration_marks(c: canvas.Canvas, g: Geometry, opts: PDFOptions) -> None:
    inset = opts.reg_marks_inset_mm
    size = opts.reg_marks_size_mm
    W, H = g.page_w_mm, g.page_h_mm
    positions = [
        (inset, H - inset),
        (W - inset, H - inset),
        (inset, inset),
    ]
    c.saveState()
    c.setFillGray(0.0)
    c.setStrokeGray(0.0)
    for cx, cy in positions:
        x = (cx - size / 2) * mm
        y = (cy - size / 2) * mm
        c.rect(x, y, size * mm, size * mm, stroke=0, fill=1)
    c.restoreState()


def _draw_footer(
    c: canvas.Canvas,
    g: Geometry,
    page_num: int,
    page_total: int,
    kind: str,
    chunk_idx: int,
) -> None:
    c.saveState()
    c.setFillGray(0.55)
    c.setFont("Helvetica", 6.5)
    label = "Fronts" if kind == "front" else "Backs"
    footer = f"MPC Forge  ·  {label} · hoja {page_num}/{page_total} ·  grid {g.cols}×{g.rows}"
    c.drawRightString((g.page_w_mm - 6) * mm, 4 * mm, footer)
    c.restoreState()
