# Changelog

Todos los cambios notables se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) y el proyecto usa
versionado semántico ([SemVer](https://semver.org/lang/es/)).

## [Unreleased]

### Added
- Empaquetado con PyInstaller (modo `--onedir`).
- Workflow de GitHub Actions que compila el `.exe` al pushear un tag `v*`.

#### Roadmap Fase 1 — inspirado en `chilli-axe/mpc-autofill`

- **Búsqueda de artes con asciifolding**: el nombre normalizado ahora reduce
  diacríticos ("Jayā" ↔ "Jaya") y ligaduras ("Æther" ↔ "Aether") vía
  `unicodedata` (sin dependencias nuevas). La misma pipeline se aplica al
  query del usuario en `gdrive_search.search()`. Un backfill idempotente
  recalcula `IndexedArt.name_normalized` sobre el índice existente al
  arrancar, sin obligar a reindexar cada drive
  (`gdrive_indexer.NORMALIZATION_VERSION` + `backfill_normalized_names()`).
- **Cache local de DFC pairs (transform, MDFC, meld)** precomputado desde
  Scryfall. Nueva tabla `dfc_pairs` (~1500 filas) sincronizada semanalmente
  en background dentro del `lifespan`, con reemplazo transaccional que
  preserva los datos previos si Scryfall falla. Endpoints:
  `GET /api/dfc-pairs/stats`, `POST /api/dfc-pairs/sync?force=true&wait=true`.
- **Import unificado desde múltiples sitios (`ImportSite` abstracto)**: nueva
  jerarquía `mpc_forge/clients/import_sites/` con auto-registro por
  `__init_subclass__`. Sitios soportados: Moxfield, Archidekt, TappedOut,
  MTGGoldfish, Scryfall, CubeCobra. Nuevo endpoint
  `POST /api/decks/import/url` que detecta el sitio por hostname y pasa
  todo por la pipeline común de texto plano.
  `GET /api/decks/import/supported-sites` lo consume el frontend para
  pintar chips y detectar el sitio antes del submit. La ruta antigua
  `POST /api/decks/import/moxfield` se mantiene para retro-compatibilidad.
- **Tags en el índice de drives**: cada `IndexedArt` guarda ahora `tags`
  (CSV canónico) más ocho booleanos indexados (`is_full_art`, `is_borderless`,
  `is_extended`, `is_showcase`, `is_retro`, `is_textless`, `is_promo`,
  `is_alt_art`). La extracción reconoce vocabulario canónico con aliases
  ("FA"→"full art", "BL"→"borderless", …) sobre paréntesis y corchetes del
  filename y del folder path. El backfill los rellena para filas ya
  indexadas sin reindexar. `gdrive_search.search()` acepta `tags_include` y
  `tags_exclude`; el endpoint `GET /api/drives/search` los expone como
  query strings CSV. Nuevo panel de filtros en el art picker de `deck.html`
  con debounce y refetch automáticos. Badges nuevos en thumbnails de drives:
  `BRDLESS`, `FULL`, `RETRO`, `PROMO`, `EXT`, `SHOW`, `ALT`.

### Changed
- `templates/index.html`: el panel "Desde Moxfield" pasa a "Desde URL"
  genérico con auto-detección, chips de sitios soportados y validación
  local del hostname antes del submit.

### Database

Cambios retrocompatibles (`SCHEMA_VERSION` sigue en `"8"`, no destruye datos):

- Nueva tabla `dfc_pairs` (creada por `Base.metadata.create_all`).
- Nuevas columnas en `indexed_art`: `tags`, `is_full_art`, `is_borderless`,
  `is_extended`, `is_showcase`, `is_retro`, `is_textless`, `is_promo`,
  `is_alt_art` — añadidas vía `ADD COLUMN` idempotente en `init_db()`.
- Nuevos índices parciales sobre `indexed_art(name_normalized) WHERE
  is_<flag>=1` para acelerar los filtros por tag, y un índice
  `lower(front_name)` sobre `dfc_pairs` para lookups case-insensitive.


## [0.2.0] — 2026-08-22

Primera versión funcional completa.

### Added
- Importación de mazos desde Moxfield (API v3 → v2 → cloudscraper) y desde texto plano.
- Editor de mazos con:
  - Agrupación por rol (commander / mainboard / sideboard / tokens / meld_result).
  - Autocomplete de Scryfall con navegación por teclado.
  - Cambio de arte (frentes y reversos DFC/MDFC/meld) desde galería de impresiones oficiales.
  - Preferencias globales de arte (recordar impresión para una carta en todos los mazos).
  - Detección y añadido automático de reversos meld/DFC.
  - Mana pips oficiales, badges DFC/MDFC/MELD/BATTLE, preview con Ctrl+hover.
- Custom art:
  - Rescan automático de carpeta `custom_art/` local.
  - Añadir por URL (con soporte específico de Google Drive).
  - Reconocimiento de convención `Card Name [BACK] - Variant.ext`.
- Fuzzy search en Google Drives comunitarios:
  - Catálogo curado de 67 drives de MPCFill pre-sembrado.
  - Indexado con Google Drive API v3 (recomendado) o scraping HTML como fallback.
  - Búsqueda instantánea con normalización agresiva y scoring por `noise_ratio`.
- Exportación:
  - XML compatible con MPC Autofill (incluye `<backs>` para cardback global).
  - PDF 3×3 tamaño real (63×88 mm) para impresión doméstica, A4 y Letter.
  - Estimador de coste con conversión USD→EUR + shipping configurable.
- Integración MPC Autofill: detección del `.exe` local y lanzamiento con `--directory`.
- Historial de tiradas de impresión.
- Sistema de settings runtime (edita desde la UI, se aplican sin reiniciar):
  - Tipo de cambio USD/EUR, envío internacional, User-Agent, cardstock por defecto,
    preferencias de arte, ruta al `.exe` de MPC Autofill, Google API key.
- Log temporal a fichero:
  - `%APPDATA%\MPC-Forge\logs\mpc-forge.log`.
  - Se borra al cerrar limpiamente, se conserva si crashea.
  - Descargable desde la UI para diagnóstico.
- Backup manual de BD.
- SQLite en modo WAL con `busy_timeout=30s` — soporta indexado concurrente sin
  errores `database is locked`.
