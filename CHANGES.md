# PDF Studio — delta para MPC Forge

Reemplaza el flujo antiguo de PDF por un **PDF Studio** inspirado en
proxxied.com/app. Diseño de 3 columnas con preview SVG en vivo con scroll
vertical y controles de guías granulares.

Iteraciones: **v1** (primera entrega) → **v2** (paridad completa con
proxxied + petición de Noel + export ZIP de imágenes).

---

## Cambios v2

### 1. Export ZIP de imágenes individuales
Botón "Export ZIP de imágenes" en el panel izquierdo. Endpoint nuevo
`POST /api/decks/{id}/export-images` que produce un ZIP con:
- Una imagen por carta única, nombrada como el nombre oficial de Scryfall,
  saneada para el sistema de archivos (Windows/macOS/Linux). DFC:
  `<cara_frontal>.png` + `<cara_trasera>.png`.
- `decklist.txt` en formato Moxfield-compatible (importable en Moxfield,
  Archidekt, MTGGoldfish).
- `README.txt` explicando el contenido.

Comparte el mismo pipeline de `resolve_deck_for_xml`
(caché de arte reutilizada, progreso vía polling).

### 2. Página independiente fronts vs backs — con offsets propios
Dos campos nuevos en Posicionamiento avanzado:
- `back_offset_x_mm` / `back_offset_y_mm` — se suman **solo** a las páginas
  de reversos, para compensar la deriva de la impresora al voltear el
  papel en duplex.

### 3. Nuevo `backs_content: "all_cards"` (feedback de Noel)
> "Si le pones el dorsal en las páginas intercaladas y le añades reborde
> de 1mm a todas las cartas y traseras, sin problema."

Antes, la modalidad Duplex solo ponía reversos de DFC (dejaba huecos en
el resto). Ahora hay un selector explícito:
- **Todas las cartas** (nuevo, default): DFC → cara B, resto → cardback
  estándar de MTG. Es lo que quiere Noel para proxy real.
- **Solo DFC**: comportamiento v1 (deja hueco en cartas sin reverso).

El bleed (por ejemplo 1 mm que pedía Noel) ya se aplicaba a ambas caras.

Preset nuevo "Duplex all-backs" que activa esta modalidad + bleed 1mm.

### 4. Guías 100% correctas — paridad con proxxied
Refactor completo separando en **dos ejes independientes**:

**Guías por carta** (marcas alrededor de cada carta)
- Estilo: `Esquinas` o `Rectángulo`
- **Forma: `Cuadrado` o `Redondeado`** (nuevo — arcos bezier)
- **Trazo: `Sólido` / `Discontinuo` / `Puntos`** (era binario)
- Colocación: `Fuera` / `Medio` / `Dentro`
- Largo, color, grosor

**Guías de página** (líneas que atraviesan la página — panel nuevo):
- `Ninguna`
- `Líneas completas` — atraviesan la página en cada corte
- `Solo en márgenes` — solo tramos fuera del grid (limpio, para
  guillotina sin ensuciar el interior)

**Hide-flags de duplex** (panel nuevo, 4 checkboxes):
- Ocultar guías por carta en frentes
- Ocultar guías de página en frentes
- Ocultar guías por carta en reversos
- Ocultar guías de página en reversos

Útil si vas a cortar por una sola cara y no quieres que las marcas se
transparenten al otro lado.

### 5. Scroll vertical en vez de paginación
Antes: botones `<` / `>` para pasar página, se veía una hoja a la vez.
Ahora: todas las hojas apiladas verticalmente, se scrollean con el ratón.
Cada hoja lleva encima su label (`HOJA 3 · REVERSOS (espejado)`).
Se ha eliminado la barra de paginación; queda solo el zoom y el toggle
de márgenes.

### 6. Endpoint auxiliar `/api/cardback`
Sirve la imagen del cardback estándar para que el preview del studio pueda
pintarlo en las páginas de reversos cuando el modo es `all_cards`. El PDF
real siempre carga el fichero desde disco directamente (no toca este
endpoint), así que si no hay cardback configurado el preview queda con
huecos pero el PDF se genera igual con lo que haya.

---

## Correcciones de v1 (para referencia)

- **Alpine no puede usar `<template x-if>` / `<template x-for>` dentro de
  `<svg>`.** El parser HTML crea un `SVGElement` (no un
  `HTMLTemplateElement`) y Alpine peta con *"e.content is undefined"* /
  *"Document.importNode: Argument 1 is not an object"*. Solución: el
  preview genera todo el SVG como string en JS (getter `svgMarkupForPage`)
  y se inyecta con `x-html`.
- Guards `x-show` + optional-chain en las expresiones que leen
  `currentPage.kind` durante el race de init.

---

## Compatibilidad y ficheros modificados

**Ficheros modificados:**
- `mpc_forge/services/pdf_generator.py` — reescrito con extended PDFOptions
- `mpc_forge/services/deck_activity.py` — `IMAGES_EXPORTED` kind añadida
- `mpc_forge/routes/export.py` — nuevo BuildPDFRequest, endpoint
  `/export-images`, endpoint `/cardback`, media type ZIP
- `mpc_forge/routes/ui.py` — ruta `/decks/{id}/pdf` (v1)
- `templates/pdf_studio.html` — full rewrite
- `templates/deck.html` — botón PDF Studio + quick-build (v1)

**Ficheros nuevos:**
- `mpc_forge/services/image_export.py`

**Compatibilidad legacy:** El endpoint `POST /decks/{id}/build-pdf` sigue
aceptando los nombres antiguos (`cut_marks`, `gap_mm`, `guides_enabled`,
`guides_style`, `guides_stroke`, `guides_placement`, `guides_length_mm`,
`guides_color`, `guides_width_pt`) y los traduce internamente al modelo
nuevo. Scripts externos que llamen a la API no se rompen.

**localStorage:** las opciones se persisten bajo la clave
`pdfStudio.opts.v2.<deck_id>` (nueva clave "v2" para no re-cargar presets
antiguos con campos obsoletos). Al abrir el studio por primera vez tras
actualizar, sales con los defaults limpios.

**Tests:** 59/59 pasan sin cambios.

---

## Cómo aplicarlo

Descomprimir sobre la raíz del proyecto:

```bash
unzip -o mtg-forge-pdf-studio.zip -d /ruta/a/mtg-forge/
```

No hay migraciones de BD, no hay dependencias nuevas. Solo restart del
servidor:

```bash
python -m mpc_forge.app
```

Abre cualquier mazo → clic en **PDF Studio** en el header → probar.
