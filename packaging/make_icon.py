"""Genera el icono de MPC Forge en formato .ico (multi-resolución) y PNG.

Diseño:
- Fondo cuadrado redondeado con gradiente arcane oscuro (azul-morado).
- Borde dorado sutil (paleta accent que usamos en la app).
- Letra "F" dorada centrada, tipografía serif Bold.
- 5 puntos WUBRG discretos en la base (guiño MTG sin sobrecargar).
- Se genera en 16/32/48/64/128/256 px para el .ico multi-resolución.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

BG_TOP        = (26, 30, 42)
BG_BOTTOM     = (10, 13, 19)
GOLD          = (212, 175, 55)
GOLD_BRIGHT   = (233, 200, 106)
GOLD_DIM      = (160, 130, 40)

MTG_COLORS = [
    (245, 240, 216),
    (91,  155, 213),
    (58,  58,  74),
    (217, 83,  79),
    (92,  184, 92),
]


def make_icon(size: int) -> Image.Image:
    """Genera el icono para un tamaño dado (cuadrado). Devuelve RGBA."""
    scale = 2 if size >= 32 else 1
    s = size * scale

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    corner_radius = int(s * 0.18)

    draw.rounded_rectangle(
        [(0, 0), (s - 1, s - 1)],
        radius=corner_radius,
        fill=BG_BOTTOM,
    )

    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        grad_draw.line([(0, y), (s, y)], fill=(r, g, b, 255))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (s - 1, s - 1)], radius=corner_radius, fill=255,
    )
    img.paste(grad, (0, 0), mask)

    if size >= 48:
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        cx, cy = int(s * 0.30), int(s * 0.20)
        max_r = int(s * 0.7)
        for r in range(max_r, 0, -8):
            alpha = int(28 * (1 - r / max_r))
            gd.ellipse([cx - r, cy - r, cx + r, cy + r],
                       fill=(*GOLD, alpha))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(s * 0.05)))
        img.alpha_composite(glow)
        clipped = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        clipped.paste(img, (0, 0), mask)
        img = clipped
        draw = ImageDraw.Draw(img)

    border_width = max(1, int(s * 0.012))
    draw.rounded_rectangle(
        [(border_width // 2, border_width // 2), (s - 1 - border_width // 2, s - 1 - border_width // 2)],
        radius=corner_radius - border_width // 2,
        outline=GOLD_DIM,
        width=border_width,
    )

    font_size = int(s * 0.68)
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    ]
    font = None
    for fp in font_paths:
        if Path(fp).exists():
            font = ImageFont.truetype(fp, font_size)
            break
    if font is None:
        font = ImageFont.load_default()

    text = "F"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (s - text_w) // 2 - bbox[0]
    y = (s - text_h) // 2 - bbox[1] - int(s * 0.04)

    if size >= 32:
        shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((x + int(s*0.01), y + int(s*0.01)), text,
                                     fill=(0, 0, 0, 120), font=font)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=int(s * 0.008)))
        img.alpha_composite(shadow)

    draw = ImageDraw.Draw(img)
    draw.text((x, y), text, fill=GOLD_BRIGHT, font=font)

    if size >= 48:
        dot_r = max(2, int(s * 0.028))
        gap = int(dot_r * 1.6)
        total_w = 5 * (dot_r * 2) + 4 * gap
        start_x = (s - total_w) // 2 + dot_r
        y_dots = int(s * 0.86)
        for i, color in enumerate(MTG_COLORS):
            cx = start_x + i * (dot_r * 2 + gap)
            draw.ellipse(
                [cx - dot_r, y_dots - dot_r, cx + dot_r, y_dots + dot_r],
                fill=color,
                outline=(*GOLD_DIM, 180),
                width=max(1, int(s * 0.004)),
            )

    if scale > 1:
        img = img.resize((size, size), Image.Resampling.LANCZOS)

    return img


def main() -> None:
    out_dir = Path("packaging")
    out_dir.mkdir(exist_ok=True)

    sizes = [16, 32, 48, 64, 128, 256]
    images = [make_icon(s) for s in sizes]

    ico_path = out_dir / "icon.ico"
    images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    print(f"OK  {ico_path}  ({ico_path.stat().st_size} bytes, {len(sizes)} resoluciones)")

    png_path = Path("static") / "logo.png"
    png_path.parent.mkdir(exist_ok=True)
    make_icon(512).save(png_path, format="PNG", optimize=True)
    print(f"OK  {png_path}  ({png_path.stat().st_size} bytes, 512x512)")

    favicon_path = Path("static") / "favicon.png"
    make_icon(32).save(favicon_path, format="PNG", optimize=True)
    print(f"OK  {favicon_path}  ({favicon_path.stat().st_size} bytes, 32x32)")

    preview_dir = Path("packaging") / "icon-previews"
    preview_dir.mkdir(exist_ok=True)
    for s in sizes:
        p = preview_dir / f"icon-{s}.png"
        make_icon(s).save(p)
        print(f"     preview: {p}")


if __name__ == "__main__":
    main()
