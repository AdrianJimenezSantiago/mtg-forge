# PDF Studio — delta para MPC Forge

Reemplaza el flujo antiguo de PDF por un **PDF Studio** inspirado en
proxxied.com/app. Diseño de 3 columnas con preview SVG en vivo con scroll
vertical y controles de guías granulares.

Iteraciones: **v1** → **v2** (paridad proxxied + Noel) → **v3** (cardback
por mazo + fix HEAD 405).

---

## Cambios v3

### 1. Fix: `HEAD /api/cardback` devolvía 405
FastAPI por defecto solo acepta el verbo del decorador. Se cambió `@router.get`
por `@router.api_route(..., methods=["GET", "HEAD"])`. El frontend hace un
HEAD para detectar si hay cardback global disponible sin descargar el binario.

### 2. Cardback global por mazo (feature "Sefirot")
Se puede definir un **cardback específico de este mazo** que sustituye al
`default-back` en todas las cartas EXCEPTO en las que ya tienen su propio
reverso (DFC / MDFC / meld — esas siguen usando su cara-B).

**UI**: en el panel "Reversos" del PDF Studio aparece una sección nueva
"Cardback global del mazo" con:
- Thumbnail del cardback actual
- Botón "Cambiar cardback…" que abre un modal
- Botón "Volver al cardback global" si el mazo ya tiene uno custom

**Modal**: reutiliza los tres flujos de arte alternativo:
- **Añadir imagen desde URL** — pega un enlace de Google Drive o cualquier
  URL de imagen. Llama a `POST /api/custom-art/from-url` (el mismo endpoint
  que ya usaba el picker de arte de cartas).
- **Buscar en Google Drives indexados** — search-box con debounce, mismo
  endpoint `GET /api/drives/search` que usa el picker de arte de cartas.
- **Tu librería de artes custom** — grid con todos los custom arts locales
  (`GET /api/custom-art/`).

Al seleccionar cualquiera, se descarga a la librería local (si aún no está)
y se asigna al mazo vía `PUT /api/decks/{id}/cardback-settings`.

**Backend nuevo**:
- Columna `Deck.custom_cardback_art_id` (FK → CustomArt, `SET NULL` on delete)
- Migración SQLite idempotente en `init_db()` que hace `ALTER TABLE ADD COLUMN`
  solo si no existe — **NO destruye mazos existentes** al actualizar
- 3 endpoints REST:
  - `GET  /api/decks/{id}/cardback-settings` — estado actual
  - `PUT  /api/decks/{id}/cardback-settings` — asignar CustomArt.id (o null)
  - `DELETE /api/decks/{id}/cardback-settings` — resetear al global
- `build_pdf(..., cardback_path_override=...)` acepta la ruta del cardback
  específico. Si el CustomArt referenciado se ha borrado del disco, cae
  automáticamente al `default_cardback_path()` global.
- `build_images_zip(..., cardback_path=...)` incluye el cardback en el ZIP
  como `_cardback.<ext>` (guion bajo para que quede al principio del
  listado ordenado).

**Comportamiento con DFC/MDFC/meld**: sin cambios. La lógica existente en
`_expand_slots` respeta `c.back_path` — solo se aplica el cardback custom
en slots donde `back_path` es `None`. Meld cards resueltas con back_path
poblado (via `meld_result` de Scryfall) mantienen su cara-B propia.

---

## Cambios v2 (previos)

### Export ZIP de imágenes individuales
Botón "Export ZIP de imágenes" que produce un ZIP con una imagen por
carta única (nombrada por Scryfall, saneada FS), reversos DFC por su
cara-B, `decklist.txt` Moxfield-compatible y `README.txt`. Endpoint
`POST /api/decks/{id}/export-images`.

### Página independiente fronts vs backs con offsets propios
`back_offset_x_mm` / `back_offset_y_mm` — se suman solo a páginas de reversos.

### `backs_content: "all_cards"` (feedback de Noel)
> "Si le pones el dorsal en las páginas intercaladas y le añades reborde
> de 1mm a todas las cartas y traseras, sin problema."

Modo (default) que rellena TODOS los slots de reversos con el cardback
(DFC → cara B, resto → cardback estándar o el del mazo si se ha configurado).

### Guías 100% correctas — paridad con proxxied
Refactor en dos ejes independientes:
- **Card guides**: Estilo (Esquinas/Rectángulo) + Forma (Cuadrado/Redondeado, arcos bezier) + Trazo (Sólido/Discontinuo/Puntos) + Colocación + Largo/Color/Grosor
- **Page guides**: Ninguna / Líneas completas / Solo en márgenes
- **Hide-flags de duplex**: 4 checkboxes para ocultar cada eje en cada cara

### Scroll vertical en vez de paginación
Todas las hojas apiladas verticalmente en la zona central.

### Endpoint auxiliar `/api/cardback`
Sirve la imagen del cardback estándar para el preview.

---

## Correcciones de v1 (previas)

- **Alpine `<template>` dentro de `<svg>`**: reemplazado por generación de
  SVG como string + `x-html`.
- Guard `x-show` + optional-chain en el race de init de `currentPage.kind`.

---

## Compatibilidad y ficheros modificados

**Modificados en v3:**
- `mpc_forge/models.py` — columna `Deck.custom_cardback_art_id` (FK)
- `mpc_forge/db.py` — migración idempotente ADD COLUMN
- `mpc_forge/routes/export.py` — endpoints cardback-settings, fix HEAD 405,
  helper `_resolve_deck_cardback`, cardback pasado a build_pdf y ZIP
- `mpc_forge/services/pdf_generator.py` — nuevo param `cardback_path_override`
- `mpc_forge/services/image_export.py` — nuevo param `cardback_path`, incluye `_cardback.<ext>` en el ZIP
- `templates/pdf_studio.html` — sección cardback en panel Reversos + modal picker

**Sin cambios respecto a v2:**
- `mpc_forge/services/deck_activity.py`
- `mpc_forge/routes/ui.py`
- `templates/deck.html`

**Migración de BD**: no destructiva. Al arrancar, `init_db()` detecta que
falta la columna `custom_cardback_art_id` y la añade con `ALTER TABLE ADD
COLUMN`. Los mazos existentes se conservan. Verificado empíricamente.

**Compatibilidad legacy API**: el endpoint `POST /decks/{id}/build-pdf` sigue
aceptando los nombres antiguos (`cut_marks`, `gap_mm`, `guides_*`) y los
traduce internamente.

**localStorage**: `pdfStudio.opts.v2.<deck_id>` (v3 no cambia el shape).

**Tests**: 59/59 pasan sin cambios.

---

## Cómo aplicarlo

```bash
unzip -o mtg-forge-pdf-studio.zip -d /ruta/a/mtg-forge/
python -m mpc_forge.app
```

No hay migraciones manuales (se aplica sola). No hay dependencias nuevas.

Abre cualquier mazo → clic en **PDF Studio** en el header → activa
"Reversos" → aparece la sección del cardback. Clic en "Cambiar cardback…"
para abrir el picker.

