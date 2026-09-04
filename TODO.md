# TODO — Roadmap MTG Forge

Lista viva de sugerencias detectadas durante la implementación del roadmap.
Todos los items P2 y P3 acumulados durante las fases 1-3 se han procesado
en la sesión "Extras". Ver `CHANGELOG.md` para el resumen.

## Bugs encontrados y ya corregidos en esta sesión

- **[FIXED] `recommend-by-artist` usaba `printing_id`** (que no existe) en
  vez de `scryfall_id`. Además hacía un JOIN innecesario a `PrintingCache`
  cuando `DeckCard.oracle_id` ya existe directamente. Corregido durante el
  smoke test con el deck real de Moxfield.
- **[FIXED] Path duplicado en `/api/decks/decks/{deck_id}/recommend-by-artist`**
  por prefijo repetido en `@router.post`. Corregido durante smoke test.
- **[FIXED] `preview_print_runs` descargaba TODO el arte del mazo** llamando
  a `resolve_deck_for_xml` — el endpoint tardaba minutos y fallaba si algún
  arte no estaba cacheado. Refactor: nueva función ligera `plan_deck_slots()`
  que consulta solo la BD, sin red. Ahora responde en <100ms incluso para
  mazos grandes.
- **[FIXED] Test `test_settings_endpoint_returns_new_groups` desactualizado**
  tras añadir el grupo "Búsqueda avanzada" en Fase 2. Actualizado.
- **[FIXED] `_get_scryfall(request)` sin type hint** hacía que FastAPI
  interpretase `request` como query-param obligatorio. Añadido `Request`
  en el hint + import correspondiente.

## Extras — TODOs procesados (sesión de cleanup)

Todos los items marcados con prioridad P2/P3 acumulados en las 3 fases se
han procesado. Referencias: [F1|F2|F3]/[T1..T11].

### Backend

- **[FIXED] F1/T2** — Cache local DFC consultado en `import_from_plaintext`;
  revierte automáticamente nombres de reverso (ej. "Insectile Aberration")
  a su front (Delver of Secrets) evitando unresolved.
- **[FIXED] F1/T3** — Parser plaintext refactorizado como state machine:
  respeta `//Commanders`, `//Mainboard`, `//Sideboard`, prefijo MTGO `SB:`,
  y defensa contra líneas ambiguas como "4 Sideboard" (carta con qty 4, no
  cabecera). Compatible con MTGA, MTGO, Deckstats, Moxfield.
- **[FIXED] F1/T3** — `DeckstatsSite` implementado (usa el endpoint
  `?export_txt=1`). MagicVille omitido conscientemente por no poder
  verificar el schema HTML — TODO ampliable.
- **[FIXED] F1/T4** — Vocabulario de tags editable desde
  `<data_dir>/tag_vocabulary.json`, con endpoints
  `POST /api/tag-vocabulary/reload` y `GET /api/tag-vocabulary/current`.
- **[FIXED] F1/T4** — Segmentos de folder sin brackets también se
  reconocen como tags si matchean EXACTO un alias (ej. carpeta "Full Art/"
  → tag full_art). `NORMALIZATION_VERSION` bumpeada a 5 para reprocesar.
- **[FIXED] F2/T5** — `services/canonical.py` con `validate_and_enrich()`
  que consulta Scryfall en batches de 75. Endpoints
  `POST /api/drives/canonical/validate` y `GET /api/drives/canonical/details`.
  Poblan `PrintingCache` para lookups posteriores del picker.
- **[FIXED] F2/T6** — Endpoint `POST /api/drives/rebuild-fts5` fuerza
  rebuild manual de la tabla virtual FTS5 (útil si triggers fallaron).
- **[FIXED] F2/T7** — Endpoint `POST /api/art-sources/validate` con
  auto-detección de tipo o forzado. UI de settings actualizada con
  selector y feedback visual.
- **[FIXED] F2/T7** — Columnas `download_url` y `thumb_url` en
  `IndexedArt` para tipos no-gdrive (URLs directas cacheadas).
- **[FIXED] F2/T8** — `_default_thumb_url` con dispatch por `source_type`
  usando el registry, con `ArtSource` opcional pre-loaded para batches.
- **[FIXED] F2/T8** — pHash calculado inline en `_index_generic` cuando
  `phash.enabled` está activo. Degrada silencioso sin Pillow.
- **[FIXED] F2/T8** — Endpoint `POST /api/drives/phash/compute-all` para
  retrofit sobre TODOS los sources (incluido gdrive).
- **[FIXED] F3/T7** — `S3SourceType` para buckets S3 públicos y Cloudflare
  R2. Auto-detección de URLs `s3://`, `.s3.amazonaws.com`,
  `.r2.cloudflarestorage.com` en `_detect_source_type`.
- **[FIXED] F3/T9** — `preprocess_cards()` en `post_processing.py` aplica
  dpi/sharpen/cmyk/rotate_backs_180 vía Pillow a archivos temp antes de
  que el PDF los lea. Cableado al PDF con flag `use_deck_post_processing`.
- **[FIXED] F3/T9** — Setting global `default_post_processing` en
  `<data_dir>/default_post_processing.json` con endpoints CRUD
  `GET/PUT/DELETE /api/settings/default-post-processing`. Fallback cuando
  el mazo no tiene config propia (usado en `build-split-xml` y PDF).
- **[FIXED] F3/T10** — Cardback custom del mazo preservado en split XML
  vía `_resolve_deck_cardback` (antes se usaba el global).
- **[FIXED] F3/T10** — DP solver `suggest_tier_combination()` +
  `split_into_runs_optimized()` para minimizar wasted slots. Flag
  `?optimize=true` en `/decks/{id}/print-runs/preview`.
- **[FIXED] F3/T11** — Cache de artists en tabla `oracle_artists` con TTL
  7 días. `_lookup_cache` y `_persist_cache` en `recommend_by_artist`
  aceleran de ~50s a <100ms para mazos ya vistos.
- **[FIXED] F3/T11** — `recommend_by_style()` con criterios
  set/borderless/showcase/extended/full_art combinables con AND.
  Endpoint `POST /decks/{id}/recommend-by-style`.

### Frontend

- **[FIXED] F2/T5** — Input "Set canónico" en el picker filtra por
  `expansion_code` del drive.
- **[FIXED] F2/T7** — Selector `source_type` con placeholder dinámico +
  botón "Validar URL" + feedback visual en el form add source.
- **[FIXED] F2/T8** — Modal "Ver similares" (pHash) con botón cadenas
  flotante en cada thumbnail de drive con hash calculado.
- **[FIXED] F2/T8** — Toggle "Colapsar duplicados" en el picker: agrupa
  artes de drives con el mismo `image_hash` mostrando badge "+N iguales".
- **[FIXED] F3/T9** — Panel "Post-processing del mazo" en PDF Studio con
  toggle que envía `use_deck_post_processing` al build-pdf. Muestra
  resumen de los efectos activos del mazo.
- **[FIXED] F3/T10** — Modal "Print runs" en el mazo con vista previa de
  la partición (greedy vs optimized), grid con cartas por run,
  cardstock/coste/wasted por run, y botón "Generar los N XMLs" que
  dispara `/build-split-xml`.
- **[FIXED] F3/T11** — Botón "Aplicar arte de X a N cartas" (users icon)
  en cada thumbnail con artist. Abre modal con las cartas del mazo que
  tienen impresiones del mismo artista, checkboxes por carta, y aplica
  cambios via `change-art` iterando.

## Pendiente futuras iteraciones (post-Extras)

### Optimizaciones y features
- Cachear "mejor printing por artist" además del listado (evitar re-fetch
  al confirmar el arte tras cache hit).
- Endpoint bulk `change-art-batch` para reemplazar N artes en una sola
  llamada (hoy el modal "Aplicar arte de X" itera call-por-carta).
- MagicVille ImportSite — requiere acceso al sitio para validar HTML/API.
- pHash: `list_files` de gdrive no calcula pHash inline (bandwidth); usar
  el endpoint `/phash/compute-all` como retrofit tras indexado.

### Formato
- Los `raw_line` de entries no resueltos se muestran verbatim al usuario;
  añadir botón "sugerir corrección" con fuzzy matching contra los cache
  de Scryfall.

## Formato de este archivo

Cada item mantiene:
- **[TODO]** o **[FIXED]** al inicio.
- Referencia opcional a la fase/tarea del roadmap (Fase X · Tarea Y).
- Descripción breve del cambio + motivación.

