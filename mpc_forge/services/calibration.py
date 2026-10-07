from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mpc_forge.config import PATHS

log = logging.getLogger(__name__)

FlipEdge = Literal["long", "short"]

RULER_RANGE_MM = 15

CROSSHAIR_ARM_MM = 25

SUSPICIOUS_OFFSET_MM = 10.0


@dataclass
class CalibrationResult:
    back_offset_x_mm: float
    back_offset_y_mm: float
    flip_edge: FlipEdge
    measured_x_mm: float
    measured_y_mm: float
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "back_offset_x_mm": self.back_offset_x_mm,
            "back_offset_y_mm": self.back_offset_y_mm,
            "flip_edge": self.flip_edge,
            "measured_x_mm": self.measured_x_mm,
            "measured_y_mm": self.measured_y_mm,
            "warning": self.warning,
        }


def derive_offsets(
    measured_x_mm: float,
    measured_y_mm: float,
    *,
    flip_edge: FlipEdge = "long",
) -> CalibrationResult:
    if flip_edge == "long":
        offset_x = measured_x_mm
        offset_y = -measured_y_mm
    else:
        offset_x = -measured_x_mm
        offset_y = measured_y_mm

    warning = None
    magnitude = max(abs(measured_x_mm), abs(measured_y_mm))
    if magnitude > SUSPICIOUS_OFFSET_MM:
        warning = "calibration_offset_suspicious"
    elif magnitude == 0:
        warning = "calibration_already_aligned"

    return CalibrationResult(
        back_offset_x_mm=round(offset_x, 2),
        back_offset_y_mm=round(offset_y, 2),
        flip_edge=flip_edge,
        measured_x_mm=measured_x_mm,
        measured_y_mm=measured_y_mm,
        warning=warning,
    )


def build_sheet(
    output_path: Path | None = None,
    *,
    page_size: str = "a4",
    flip_edge: FlipEdge = "long",
) -> Path:
    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    sizes = {"a4": A4, "letter": letter}
    width, height = sizes.get(page_size.lower(), A4)

    target = output_path or (PATHS.exports_dir / "calibracion-duplex.pdf")
    target.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(target), pagesize=(width, height))
    c.setTitle("MPC Forge — Calibración de dúplex")
    c.setAuthor("MPC Forge")

    cx, cy = width / 2, height / 2

    _draw_front(c, cx, cy, width, height, mm)
    c.showPage()
    _draw_back(c, cx, cy, width, height, mm, flip_edge)
    c.showPage()
    c.save()

    log.info("Hoja de calibración generada en %s", target)
    return target


def _draw_front(c, cx, cy, width, height, mm) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(cx, height - 20 * mm, "CALIBRACIÓN DE DÚPLEX — ANVERSO")
    c.setFont("Helvetica", 9)
    c.drawCentredString(
        cx, height - 27 * mm, "Imprime esta hoja a doble cara y mírala al trasluz por este lado."
    )

    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0, 0, 0)
    arm = CROSSHAIR_ARM_MM * mm
    c.line(cx - arm, cy, cx + arm, cy)
    c.line(cx, cy - arm, cx, cy + arm)
    c.circle(cx, cy, 3 * mm, stroke=1, fill=0)

    _draw_ruler(c, cx, cy, mm, horizontal=True)
    _draw_ruler(c, cx, cy, mm, horizontal=False)

    _draw_corner_marks(c, width, height, mm)

    c.setFont("Helvetica", 8)
    c.drawCentredString(cx, 18 * mm, "Las reglas están numeradas en milímetros desde el centro.")


def _draw_back(c, cx, cy, width, height, mm, flip_edge: FlipEdge) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(cx, height - 20 * mm, "CALIBRACIÓN DE DÚPLEX — REVERSO")
    c.setFont("Helvetica", 9)
    label = "borde largo" if flip_edge == "long" else "borde corto"
    c.drawCentredString(cx, height - 27 * mm, f"Configura tu impresora para voltear por {label}.")

    c.setLineWidth(0.6)
    c.setDash(3, 2)
    arm = CROSSHAIR_ARM_MM * mm
    c.line(cx - arm, cy, cx + arm, cy)
    c.line(cx, cy - arm, cx, cy + arm)
    c.setDash()
    c.circle(cx, cy, 5 * mm, stroke=1, fill=0)

    _draw_corner_marks(c, width, height, mm)

    c.setFont("Helvetica", 8)
    c.drawCentredString(cx, 24 * mm, "Mide cuánto se desplaza ESTA cruz respecto a la del anverso.")
    c.drawCentredString(
        cx, 18 * mm, "Derecha y arriba son positivos. Introduce los dos valores en la app."
    )


def _draw_ruler(c, cx, cy, mm, *, horizontal: bool) -> None:
    c.setLineWidth(0.3)
    c.setFont("Helvetica", 6)

    for offset in range(-RULER_RANGE_MM, RULER_RANGE_MM + 1):
        if offset == 0:
            continue
        is_major = offset % 5 == 0
        tick = (3.5 if is_major else 1.8) * mm

        if horizontal:
            x = cx + offset * mm
            c.line(x, cy, x, cy - tick)
            if is_major:
                c.drawCentredString(x, cy - tick - 5 * mm, str(offset))
        else:
            y = cy + offset * mm
            c.line(cx, y, cx + tick, y)
            if is_major:
                c.drawString(cx + tick + 1.2 * mm, y - 0.8 * mm, str(offset))


def _draw_corner_marks(c, width, height, mm) -> None:
    inset = 10 * mm
    size = 5 * mm
    c.setLineWidth(0.4)
    for x, y in (
        (inset, inset),
        (width - inset, inset),
        (inset, height - inset),
        (width - inset, height - inset),
    ):
        c.line(x - size, y, x + size, y)
        c.line(x, y - size, x, y + size)


def explain(result: CalibrationResult) -> dict[str, Any]:
    return {
        **result.to_dict(),
        "explanation_key": (
            "calibration_explain_long"
            if result.flip_edge == "long"
            else "calibration_explain_short"
        ),
        "needs_correction": (
            abs(result.back_offset_x_mm) > 0.05 or abs(result.back_offset_y_mm) > 0.05
        ),
    }
