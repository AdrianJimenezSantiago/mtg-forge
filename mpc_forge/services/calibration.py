"""Calibración de dúplex: convierte "el reverso sale torcido" en dos números.

El problema
-----------
El PDF Studio tiene ``back_offset_x_mm`` y ``back_offset_y_mm``, pero el
usuario tiene que adivinarlos. Ninguna impresora doméstica alinea perfectamente
las dos caras: al voltear la hoja hay una deriva de entre medio milímetro y
tres, distinta en cada modelo y a veces distinta según el gramaje del papel.

El resultado, sin calibrar, es la queja número uno del proxy casero: cartas con
el reverso descentrado, que se notan al instante en una funda.

La solución
-----------
Un PDF de dos páginas:

* **Anverso**: una diana en el centro exacto de la hoja, más reglas milimetradas
  horizontal y vertical numeradas desde el centro.
* **Reverso**: la misma diana, pensada para verse a través del papel al
  trasluz, con marcas de registro en las esquinas.

El usuario imprime a doble cara, mira a contraluz cuánto se ha desplazado la
diana del reverso respecto a la del anverso, lee los dos números en las reglas,
y los mete en el asistente. Este módulo hace la conversión a offsets.

La parte sutil: el signo
------------------------
Es donde se equivoca todo el mundo, y por eso lo hace el código y no el usuario.
Si el reverso aparece desplazado 2 mm hacia la derecha, hay que corregirlo
moviéndolo 2 mm a la IZQUIERDA: el offset es el opuesto de la medida.

Y en el eje horizontal hay un giro extra. En dúplex por borde largo, la hoja se
voltea sobre el eje vertical, así que el sistema de coordenadas del reverso
está espejado respecto al del anverso: lo que el usuario ve desplazado "a la
derecha" mirando la hoja del revés está, en coordenadas del PDF, desplazado a
la izquierda. Ver :func:`derive_offsets`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mpc_forge.config import PATHS

log = logging.getLogger(__name__)

FlipEdge = Literal["long", "short"]

# Alcance de las reglas, en milímetros a cada lado del centro. ±15 mm cubre con
# holgura cualquier deriva real; más allá de eso el problema es de la bandeja
# de papel, no de calibración.
RULER_RANGE_MM = 15

# Longitud del brazo de la diana.
CROSSHAIR_ARM_MM = 25

# Una deriva por encima de esto no es descalibración normal: suele significar
# que el usuario midió mal, o que la impresora está tomando el papel torcido.
SUSPICIOUS_OFFSET_MM = 10.0


@dataclass
class CalibrationResult:
    """Offsets derivados de la medición del usuario."""
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
    """Convierte lo que el usuario ha medido en los offsets del PDF.

    ``measured_x_mm``: cuánto se ha desplazado la diana del reverso respecto a
    la del anverso, mirando la hoja al trasluz por el lado del ANVERSO.
    Positivo = hacia la derecha. ``measured_y_mm``: positivo = hacia arriba.

    El offset es el opuesto de la medida —para corregir un desplazamiento hay
    que aplicar el contrario— y en el eje X hay además un cambio de signo
    cuando el volteo es por borde largo, porque el reverso está espejado
    respecto al anverso en el sistema de coordenadas del PDF.

    Con volteo por borde corto la hoja gira sobre el eje horizontal, así que el
    espejado afecta a Y en lugar de a X.
    """
    if flip_edge == "long":
        # Espejo horizontal: la corrección en X va en el mismo sentido que la
        # medida, no en el contrario.
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
    """Genera el PDF de calibración de dos páginas.

    Es una función bloqueante (ReportLab): llamar siempre con
    ``asyncio.to_thread`` desde una ruta async.
    """
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
    """Anverso: diana central y reglas milimetradas numeradas."""
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(cx, height - 20 * mm, "CALIBRACIÓN DE DÚPLEX — ANVERSO")
    c.setFont("Helvetica", 9)
    c.drawCentredString(
        cx, height - 27 * mm,
        "Imprime esta hoja a doble cara y mírala al trasluz por este lado."
    )

    # Diana central. Trazo fino: si es grueso, no se distingue a contraluz cuál
    # de las dos cruces es cuál.
    c.setLineWidth(0.4)
    c.setStrokeColorRGB(0, 0, 0)
    arm = CROSSHAIR_ARM_MM * mm
    c.line(cx - arm, cy, cx + arm, cy)
    c.line(cx, cy - arm, cx, cy + arm)
    c.circle(cx, cy, 3 * mm, stroke=1, fill=0)

    _draw_ruler(c, cx, cy, mm, horizontal=True)
    _draw_ruler(c, cx, cy, mm, horizontal=False)

    # Marcas de registro en las esquinas, a 10 mm de los bordes.
    _draw_corner_marks(c, width, height, mm)

    c.setFont("Helvetica", 8)
    c.drawCentredString(
        cx, 18 * mm,
        "Las reglas están numeradas en milímetros desde el centro."
    )


def _draw_back(c, cx, cy, width, height, mm, flip_edge: FlipEdge) -> None:
    """Reverso: la misma diana, para comparar al trasluz."""
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(cx, height - 20 * mm, "CALIBRACIÓN DE DÚPLEX — REVERSO")
    c.setFont("Helvetica", 9)
    label = "borde largo" if flip_edge == "long" else "borde corto"
    c.drawCentredString(
        cx, height - 27 * mm,
        f"Configura tu impresora para voltear por {label}."
    )

    # La diana del reverso se dibuja con trazo discontinuo para distinguirla de
    # la del anverso cuando ambas se ven superpuestas a contraluz.
    c.setLineWidth(0.6)
    c.setDash(3, 2)
    arm = CROSSHAIR_ARM_MM * mm
    c.line(cx - arm, cy, cx + arm, cy)
    c.line(cx, cy - arm, cx, cy + arm)
    c.setDash()
    c.circle(cx, cy, 5 * mm, stroke=1, fill=0)

    _draw_corner_marks(c, width, height, mm)

    c.setFont("Helvetica", 8)
    c.drawCentredString(
        cx, 24 * mm,
        "Mide cuánto se desplaza ESTA cruz respecto a la del anverso."
    )
    c.drawCentredString(
        cx, 18 * mm,
        "Derecha y arriba son positivos. Introduce los dos valores en la app."
    )


def _draw_ruler(c, cx, cy, mm, *, horizontal: bool) -> None:
    """Escala milimetrada dibujada SOBRE el brazo de la diana.

    El primer intento situaba las dos reglas como barras independientes cerca
    del centro, y se cruzaban: en el cuadrante donde se solapaban los números
    quedaban ilegibles, que es justo donde hay que leerlos.

    Ahora las marcas cuelgan de los propios brazos —hacia abajo en el
    horizontal, hacia la derecha en el vertical— así que solo coinciden en el
    origen, donde no hay números porque está el círculo.
    """
    c.setLineWidth(0.3)
    c.setFont("Helvetica", 6)

    for offset in range(-RULER_RANGE_MM, RULER_RANGE_MM + 1):
        if offset == 0:
            continue
        is_major = offset % 5 == 0
        tick = (3.5 if is_major else 1.8) * mm

        if horizontal:
            x = cx + offset * mm
            # Las marcas cuelgan hacia abajo del brazo horizontal.
            c.line(x, cy, x, cy - tick)
            if is_major:
                # 5 mm por debajo de la marca, no 3: con menos separación la
                # etiqueta "5" del eje horizontal choca con la "-5" del
                # vertical, que es justo el rango donde caen las derivas
                # reales.
                c.drawCentredString(x, cy - tick - 5 * mm, str(offset))
        else:
            y = cy + offset * mm
            # Y hacia la derecha del brazo vertical.
            c.line(cx, y, cx + tick, y)
            if is_major:
                c.drawString(cx + tick + 1.2 * mm, y - 0.8 * mm, str(offset))


def _draw_corner_marks(c, width, height, mm) -> None:
    """Cruces de registro en las cuatro esquinas.

    Sirven para detectar un problema distinto de la deriva: si las esquinas no
    coinciden pero el centro sí, la hoja está entrando girada y ningún offset
    lo arregla.
    """
    inset = 10 * mm
    size = 5 * mm
    c.setLineWidth(0.4)
    for x, y in (
        (inset, inset), (width - inset, inset),
        (inset, height - inset), (width - inset, height - inset),
    ):
        c.line(x - size, y, x + size, y)
        c.line(x, y - size, x, y + size)


def explain(result: CalibrationResult) -> dict[str, Any]:
    """Texto de apoyo para la interfaz, en claves de traducción.

    Se devuelven claves y no frases porque la app es bilingüe; la vista las
    resuelve con su diccionario.
    """
    return {
        **result.to_dict(),
        "explanation_key": (
            "calibration_explain_long" if result.flip_edge == "long"
            else "calibration_explain_short"
        ),
        "needs_correction": (
            abs(result.back_offset_x_mm) > 0.05
            or abs(result.back_offset_y_mm) > 0.05
        ),
    }
