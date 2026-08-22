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

# Paleta (misma que la app)
BG_TOP        = (26, 30, 42)         # gradiente arriba: bg-elevated cálido
BG_BOTTOM     = (10, 13, 19)         # gradiente abajo: bg-base
GOLD          = (212, 175, 55)       # accent
GOLD_BRIGHT   = (233, 200, 106)      # accent-glow
GOLD_DIM      = (160, 130, 40)       # borde dorado más apagado

# Colores WUBRG (versión ligeramente atenuada para el icono)
MTG_COLORS = [
    (245, 240, 216),   # W - white
    (91,  155, 213),   # U - blue
    (58,  58,  74),    # B - black (más claro para verse en fondo oscuro)
    (217, 83,  79),    # R - red
    (92,  184, 92),    # G - green
]


def make_icon(size: int) -> Image.Image:
    """Genera el icono para un tamaño dado (cuadrado). Devuelve RGBA."""
    # Trabajamos al doble de resolución para antialias y luego reducimos
    scale = 2 if size >= 32 else 1
    s = size * scale

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- Fondo con esquinas redondeadas + gradiente vertical (simulado) ---
    corner_radius = int(s * 0.18)

    # Base plana del fondo
    draw.rounded_rectangle(
        [(0, 0), (s - 1, s - 1)],
        radius=corner_radius,
        fill=BG_BOTTOM,
    )

    # Gradiente arriba → abajo, línea a línea, respetando esquinas redondeadas.
    # Creamos un layer temporal con el gradiente y lo masqueramos.
    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad_draw = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s  # 0 arriba, 1 abajo
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        grad_draw.line([(0, y), (s, y)], fill=(r, g, b, 255))
    # Máscara con esquinas redondeadas
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [(0, 0), (s - 1, s - 1)], radius=corner_radius, fill=255,
    )
    img.paste(grad, (0, 0), mask)

    # Sutil resplandor dorado radial desde arriba-izquierda (efecto "arcane")
    # Solo en tamaños ≥ 48 para que no se vea sucio en tamaños pequeños
    if size >= 48:
        glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        cx, cy = int(s * 0.30), int(s * 0.20)
        max_r = int(s * 0.7)
        # Círculos concéntricos con alpha decreciente
        for r in range(max_r, 0, -8):
            alpha = int(28 * (1 - r / max_r))
            gd.ellipse([cx - r, cy - r, cx + r, cy + r],
                       fill=(*GOLD, alpha))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(s * 0.05)))
        img.alpha_composite(glow)
        # Re-aplicamos máscara para no salirnos de las esquinas
        clipped = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        clipped.paste(img, (0, 0), mask)
        img = clipped
        draw = ImageDraw.Draw(img)

    # --- Borde dorado sutil ---
    border_width = max(1, int(s * 0.012))
    draw.rounded_rectangle(
        [(border_width // 2, border_width // 2), (s - 1 - border_width // 2, s - 1 - border_width // 2)],
        radius=corner_radius - border_width // 2,
        outline=GOLD_DIM,
        width=border_width,
    )

    # --- Letra F centrada ---
    # Ajustamos el tamaño de fuente al icono. La F ocupa ~65% del alto.
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
    # Centrar verticalmente subiendo un poco (los WUBRG dots van abajo)
    y = (s - text_h) // 2 - bbox[1] - int(s * 0.04)

    # Sombra sutil detrás para dar profundidad
    if size >= 32:
        shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((x + int(s*0.01), y + int(s*0.01)), text,
                                     fill=(0, 0, 0, 120), font=font)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=int(s * 0.008)))
        img.alpha_composite(shadow)

    # La F en dorado
    draw = ImageDraw.Draw(img)
    draw.text((x, y), text, fill=GOLD_BRIGHT, font=font)

    # --- Guiño MTG: 5 puntos WUBRG en la base ---
    # Solo en tamaños ≥ 48. En 16/32 quedan invisibles y sucios.
    if size >= 48:
        dot_r = max(2, int(s * 0.028))
        gap = int(dot_r * 1.6)
        total_w = 5 * (dot_r * 2) + 4 * gap
        start_x = (s - total_w) // 2 + dot_r
        y_dots = int(s * 0.86)
        for i, color in enumerate(MTG_COLORS):
            cx = start_x + i * (dot_r * 2 + gap)
            # Circulito con borde dorado tenue
            draw.ellipse(
                [cx - dot_r, y_dots - dot_r, cx + dot_r, y_dots + dot_r],
                fill=color,
                outline=(*GOLD_DIM, 180),
                width=max(1, int(s * 0.004)),
            )

    # Reducir al tamaño final si trabajamos con escala mayor
    if scale > 1:
        img = img.resize((size, size), Image.Resampling.LANCZOS)

    return img


def main() -> None:
    out_dir = Path("packaging")
    out_dir.mkdir(exist_ok=True)

    # 1. Multi-resolución para .ico (Windows los usa según contexto)
    sizes = [16, 32, 48, 64, 128, 256]
    images = [make_icon(s) for s in sizes]

    ico_path = out_dir / "icon.ico"
    # Guardamos usando la de 256 como base + append de las demás
    images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
    )
    print(f"OK  {ico_path}  ({ico_path.stat().st_size} bytes, {len(sizes)} resoluciones)")

    # 2. PNG grande para favicon web y otros usos
    png_path = Path("static") / "logo.png"
    png_path.parent.mkdir(exist_ok=True)
    make_icon(512).save(png_path, format="PNG", optimize=True)
    print(f"OK  {png_path}  ({png_path.stat().st_size} bytes, 512x512)")

    # 3. Favicon PNG 32x32 para <link rel="icon">
    favicon_path = Path("static") / "favicon.png"
    make_icon(32).save(favicon_path, format="PNG", optimize=True)
    print(f"OK  {favicon_path}  ({favicon_path.stat().st_size} bytes, 32x32)")

    # 4. Previews para inspeccionar
    preview_dir = Path("packaging") / "icon-previews"
    preview_dir.mkdir(exist_ok=True)
    for s in sizes:
        p = preview_dir / f"icon-{s}.png"
        make_icon(s).save(p)
        print(f"     preview: {p}")


if __name__ == "__main__":
    main()
