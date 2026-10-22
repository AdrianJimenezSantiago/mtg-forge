from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat

from mpc_forge.services.art.custom_art import LOW_DPI
from mpc_forge.services.printing.press_color import CmykConverter
from mpc_forge.services.printing.print_shops import MTG_CARD_H_MM, MTG_CARD_W_MM, PrintShopSpec

# Por debajo de esto la diferencia de proporción es ruido del escaneo (las PNG
# de Scryfall miden 745×1040, un pelo más anchas que 63,5×88,9) y no sangrado.
_MIN_NATIVE_BLEED_MM = 0.5
# Más allá no es una carta con sangrado sino una imagen con otra proporción.
_MAX_NATIVE_BLEED_MM = 6.0

# El JPEG se intenta con esta calidad y se baja solo si no cabe en el límite de
# la imprenta. 97 queda a ~48 dB del original sin comprimir y una carta de
# Scryfall pesa ~1,9 MB, con margen frente a los 3 MB. 4:4:4 para que el texto
# de reglas no pierda nitidez en el color.
_JPEG_QUALITIES = (97, 95, 92, 88, 84, 78)
# Desviación típica (0-255) por debajo de la cual el borde se considera liso:
# el ruido de un escaneo de Scryfall sobre negro ronda 2-6.
_UNIFORM_BORDER_SPREAD = 12.0
# Reserva para la página, el contenido y la parte del PDF que se reparte.
_PAGE_OVERHEAD_BYTES = 64_000

WARN_LOW_RESOLUTION = "low_resolution"
WARN_UNUSUAL_RATIO = "unusual_ratio"
WARN_ROTATED = "rotated"
WARN_CMYK_UNTAGGED = "cmyk_untagged"


class PressImageError(ValueError):
    pass


@dataclass
class PressImage:
    jpeg: bytes
    source_size: tuple[int, int]
    source_ppi: int
    native_bleed_mm: float
    generated_bleed: bool
    rotated: bool
    jpeg_quality: int
    warnings: list[str] = field(default_factory=list)


def native_bleed_mm(width: int, height: int) -> float | None:
    """Sangrado (mm por lado) que trae una imagen, suponiendo una carta de
    63,5×88,9 mm con el mismo sangrado en los cuatro lados.

    De (63,5 + 2b) / (88,9 + 2b) = ancho / alto se despeja b. Las imágenes de
    MPC (822×1122) dan ~3,05 mm, que es su 1/8" menos el redondeo a píxeles.
    Devuelve None si la proporción no corresponde a una carta.
    """
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    if ratio >= 1:
        return None
    bleed = (MTG_CARD_W_MM - MTG_CARD_H_MM * ratio) / (2 * (ratio - 1))
    if bleed < -_MIN_NATIVE_BLEED_MM or bleed > _MAX_NATIVE_BLEED_MM:
        return None
    return bleed if bleed >= _MIN_NATIVE_BLEED_MM else 0.0


def prepare_press_image(path: Path, spec: PrintShopSpec, converter: CmykConverter) -> PressImage:
    try:
        with Image.open(path) as opened:
            opened.load()
            img = ImageOps.exif_transpose(opened)
            icc_profile = opened.info.get("icc_profile")
    except FileNotFoundError as e:
        raise PressImageError(f"No existe la imagen {path}") from e
    except (OSError, Image.DecompressionBombError) as e:
        raise PressImageError(f"No se pudo leer la imagen {path.name}: {e}") from e

    try:
        return _prepare(img, icc_profile, spec, converter)
    except PressImageError:
        raise
    except (ValueError, OSError) as e:
        # Modos o perfiles raros que Pillow no sabe convertir: mejor un error con
        # el nombre del fichero que un 500.
        raise PressImageError(f"No se pudo preparar la imagen {path.name}: {e}") from e


def _prepare(
    img: Image.Image, icc_profile: bytes | None, spec: PrintShopSpec, converter: CmykConverter
) -> PressImage:
    source_size = img.size
    warnings: list[str] = []

    rotated = img.width > img.height
    if rotated:
        # Cartas horizontales (batallas, planos): se giran 90° en sentido horario
        # para que la página siga siendo vertical como la plantilla.
        img = img.transpose(Image.Transpose.ROTATE_270)
        warnings.append(WARN_ROTATED)

    img = _flatten(img)
    if img.mode == "CMYK" and converter.describe_source("CMYK", icc_profile) == "cmyk_untagged":
        warnings.append(WARN_CMYK_UNTAGGED)

    bleed = native_bleed_mm(*img.size)
    if bleed is None:
        warnings.append(WARN_UNUSUAL_RATIO)
        bleed = 0.0

    px_per_mm_x = img.width / (MTG_CARD_W_MM + 2 * bleed)
    px_per_mm_y = img.height / (MTG_CARD_H_MM + 2 * bleed)
    card_w_px = MTG_CARD_W_MM * px_per_mm_x
    card_h_px = MTG_CARD_H_MM * px_per_mm_y

    source_ppi = round(card_w_px / (spec.trim_w_mm / 25.4))
    if source_ppi < LOW_DPI:
        warnings.append(WARN_LOW_RESOLUTION)

    # La carta de la imagen (63,5×88,9) ocupa el corte de la imprenta (63×88),
    # igual que en el PDF de casa; el sangrado pedido se mide en esa escala.
    need_x = spec.bleed_mm * card_w_px / spec.trim_w_mm
    need_y = spec.bleed_mm * card_h_px / spec.trim_h_mm
    have_x = bleed * px_per_mm_x
    have_y = bleed * px_per_mm_y
    pad_x = math.ceil(max(0.0, need_x - have_x))
    pad_y = math.ceil(max(0.0, need_y - have_y))
    generated = pad_x > 0 or pad_y > 0
    if generated:
        img = extend_bleed(img, pad_x, pad_y)

    left = pad_x + have_x - need_x
    top = pad_y + have_y - need_y
    box = (left, top, left + card_w_px + 2 * need_x, top + card_h_px + 2 * need_y)
    img = img.resize(spec.page_px, Image.Resampling.LANCZOS, box=box, reducing_gap=3.0)

    cmyk = converter.to_cmyk(img, icc_profile)
    jpeg, quality = _encode_within(cmyk, spec.max_bytes_per_page - _PAGE_OVERHEAD_BYTES)

    return PressImage(
        jpeg=jpeg,
        source_size=source_size,
        source_ppi=source_ppi,
        native_bleed_mm=round(bleed, 2),
        generated_bleed=generated,
        rotated=rotated,
        jpeg_quality=quality,
        warnings=warnings,
    )


def extend_bleed(img: Image.Image, pad_x: int, pad_y: int) -> Image.Image:
    """Genera el sangrado que le falta a la imagen.

    Si el borde de la carta es liso (negro, blanco, plata…) se prolonga con su
    color exacto. Reflejarlo metería el marco en el sangrado en cuanto el borde
    mide menos que el sangrado pedido. Si no es liso (cartas sin borde), se
    refleja: continúa el dibujo sin las franjas que deja repetir el último píxel.
    """
    color, spread = edge_band(img)
    if spread <= _UNIFORM_BORDER_SPREAD:
        out = Image.new(img.mode, (img.width + 2 * pad_x, img.height + 2 * pad_y), color)
        out.paste(img, (pad_x, pad_y))
        return out
    return mirror_pad(img, pad_x, pad_y)


def mirror_pad(img: Image.Image, pad_x: int, pad_y: int) -> Image.Image:
    """Añade `pad_x`/`pad_y` píxeles por lado reflejando los bordes."""
    pad_x = max(0, min(pad_x, img.width))
    pad_y = max(0, min(pad_y, img.height))
    if not pad_x and not pad_y:
        return img
    w, h = img.size
    out = Image.new(img.mode, (w + 2 * pad_x, h + 2 * pad_y))
    out.paste(img, (pad_x, pad_y))
    if pad_x:
        flip = Image.Transpose.FLIP_LEFT_RIGHT
        out.paste(img.crop((0, 0, pad_x, h)).transpose(flip), (0, pad_y))
        out.paste(img.crop((w - pad_x, 0, w, h)).transpose(flip), (pad_x + w, pad_y))
    if pad_y:
        flip = Image.Transpose.FLIP_TOP_BOTTOM
        full_w = out.width
        out.paste(out.crop((0, pad_y, full_w, 2 * pad_y)).transpose(flip), (0, 0))
        out.paste(out.crop((0, h, full_w, pad_y + h)).transpose(flip), (0, pad_y + h))
    return out


def _flatten(img: Image.Image) -> Image.Image:
    """Deja la imagen en RGB o CMYK opacos.

    Las esquinas transparentes de Scryfall se rellenan con el color del borde de
    la carta: la imprenta corta con esquina redondeada, pero con ±1 mm de
    tolerancia un blanco ahí se vería.
    """
    if img.mode == "CMYK":
        return img
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    if not has_alpha:
        return img if img.mode == "RGB" else img.convert("RGB")
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")
    low, _ = alpha.getextrema()
    if low == 255:
        return rgb
    color, _ = edge_band(rgb, alpha)
    flat = Image.new("RGB", rgb.size, color)
    # Máscara binaria y encogida 2 px: el antialias de la esquina y el halo claro
    # que el recorte deja junto a él dibujarían un arco tenue justo donde corta
    # la imprenta. Solo afecta a las esquinas, que es donde hay transparencia.
    opaque = alpha.point(lambda v: 255 if v == 255 else 0).filter(ImageFilter.MinFilter(5))
    flat.paste(rgb, mask=opaque)
    return flat


def edge_band(img: Image.Image, alpha: Image.Image | None = None) -> tuple[tuple[int, ...], float]:
    """Color mediano y dispersión (desviación típica del canal que más varía) de
    una franja estrecha pegada al borde de la carta, sin las esquinas ni los
    píxeles transparentes."""
    w, h = img.size
    inset = max(1, round(min(w, h) * 0.012))
    band = max(1, round(min(w, h) * 0.012))
    skip_x = round(w * 0.12)
    skip_y = round(h * 0.12)
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((skip_x, inset, w - skip_x, inset + band), fill=255)
    draw.rectangle((skip_x, h - inset - band, w - skip_x, h - inset), fill=255)
    draw.rectangle((inset, skip_y, inset + band, h - skip_y), fill=255)
    draw.rectangle((w - inset - band, skip_y, w - inset, h - skip_y), fill=255)
    if alpha is not None:
        not_opaque = alpha.point(lambda v: 0 if v == 255 else 255)
        mask.paste(0, mask=not_opaque)
    if not mask.getbbox():
        return tuple(0 for _ in img.getbands()), float("inf")
    stat = ImageStat.Stat(img, mask)
    return tuple(int(v) for v in stat.median), max(stat.stddev)


def _encode_within(img: Image.Image, budget: int) -> tuple[bytes, int]:
    data = b""
    for quality in _JPEG_QUALITIES:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True)
        data = buf.getvalue()
        if len(data) <= budget:
            return data, quality
    raise PressImageError(
        f"La imagen no baja de {len(data) // 1000} kB ni con calidad "
        f"{_JPEG_QUALITIES[-1]}; el límite por página es {budget // 1000} kB"
    )
