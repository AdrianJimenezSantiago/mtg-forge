"""Indexado de Google Drives para búsqueda de arte custom.

Dos modos:
1. **API v3** (recomendado): con GOOGLE_API_KEY configurada. Rápido, fiable,
   incluye tamaño y mime type. Cuota: 10.000 requests/día gratis.
2. **Scraping HTML** (fallback): sin API key. Parsea el HTML del
   `embeddedfolderview` que Google renderiza para carpetas públicas.
   Funciona pero es más lento, no da tamaño y algunas carpetas grandes se
   quedan cortas.

El indexado NO descarga imágenes — solo lee metadatos. La descarga solo ocurre
cuando el usuario elige "Usar este arte" en el editor de mazo, y entonces se
guarda en `custom_art/_downloaded/` como cualquier otra imagen por URL.

**Concurrencia**: SQLite serializa escrituras. Cuando el usuario pide
"Indexar todos", si lanzáramos 67 tareas en paralelo se pelearían por el lock
y muchas fallarían con "database is locked". Usamos un semáforo global para
que solo se indexe un drive a la vez, aunque el usuario lance muchos.
Se ejecutan en background secuencialmente.

Se lanza bajo demanda desde la UI (botón "Indexar" en cada drive). No hay cron
automático — el usuario decide cuándo re-indexar (ej. cuando ve que le faltan
artes nuevos).
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

# Versión del normalizador. Cuando cambiamos la lógica de `normalize_filename`,
# incrementamos este valor y el startup ejecuta un backfill idempotente que
# recalcula `IndexedArt.name_normalized` sobre todo el índice existente sin
# perder los file_ids indexados. Ver `backfill_normalized_names()`.
#
# Cambios por versión:
#   1: normalización base (lowercase, sin paréntesis, split por variante)
#   2: añadido asciifolding para manejar acentos y diacríticos.
#   3: añadida extracción de tags (`is_full_art`, `is_borderless`, …). El
#      backfill escribe tags sobre filas ya indexadas sin re-descargar.
NORMALIZATION_VERSION = 3


# Semáforo global: máximo 3 indexados concurrentes.
# Con WAL mode + busy_timeout=30s, SQLite aguanta bien 3 escritores en paralelo.
# El bottleneck real es Google Drive API (rate limit ~10 req/s por usuario), no SQLite.
# Un drive gigante (source 3 tiene decenas de miles de imágenes) puede tardar minutos;
# con concurrencia=3 los otros drives no esperan innecesariamente.
_INDEX_SEMAPHORE = asyncio.Semaphore(3)


# ---------------------------------------------------------------------------
# Nombres normalizados para fuzzy search
# ---------------------------------------------------------------------------
# Filosofía: queremos que "Forest (Full Art).png", "Forest - Alt by Chowning.png",
# y "Forest.png" TODOS se normalicen a "forest" (nombre canónico de la carta).
# En cambio "Forest Warden.png" se queda como "forest warden" — es una carta
# distinta. Así el matching exacto ya nos filtra el ruido.

_STRIP_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*[\[\(\{].*?[\]\)\}]\s*")   # elimina "(Anime)" "[BACK]" etc.
# Corta el nombre en el primer separador de variante ("-", "by", "feat", "|"):
_VARIANT_SPLIT_RE = re.compile(
    r"\s+(?:-|—|–|by|feat(?:\.|uring)?|\||//)\s+", re.IGNORECASE
)
_NONALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_MULTISPACE_RE = re.compile(r"\s+")


def _asciifold(text: str) -> str:
    """Reduce caracteres Unicode con diacríticos a su equivalente ASCII.

    Ejemplos:
      "Jayā Ballard"  → "Jaya Ballard"
      "Café"          → "Cafe"
      "naïve"         → "naive"
      "Æther Vial"    → "aether Vial"   (ligadura común en MTG)

    Estrategia: NFKD descompone caracteres en base + combining marks
    (ej. "á" → "a" + U+0301 COMBINING ACUTE ACCENT). Filtramos por
    ``unicodedata.combining()`` para descartar solo los marks, dejando
    intactos números, símbolos monetarios, etc.

    Luego traducimos manualmente ligaduras que NFKD no descompone
    (Æ, æ, Œ, œ, ß) para cubrir cartas como Æther / Aether que aparecen
    en ambas grafías según la impresión.
    """
    if not text:
        return text
    # Ligaduras que NFKD deja intactas — las mapeamos a su forma expandida.
    text = (
        text.replace("Æ", "AE").replace("æ", "ae")
        .replace("Œ", "OE").replace("œ", "oe")
        .replace("ß", "ss")
    )
    # NFKD descompone. Filtramos marks combinantes (Mn).
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_filename(name: str) -> str:
    """Convierte cualquier variante de filename al nombre canónico de la carta.

    Ejemplos:
      "Forest.png"                          → "forest"
      "Forest (Full Art).png"               → "forest"
      "Forest - Alt Art.png"                → "forest"
      "Forest by Chowning.png"              → "forest"
      "Forest [BACK].png"                   → "forest"
      "Sol Ring (Daubrez Borderless).png"   → "sol ring"
      "Bruna, the Fading Light (Women's Day).jpg" → "bruna the fading light"
      "Forest Warden.png"                   → "forest warden"  (carta distinta)

    Asciifolding (NORMALIZATION_VERSION >= 2):
      "Jayā Ballard.png"                    → "jaya ballard"
      "Æther Vial.png"                      → "aether vial"
      "Naïve Believer.png"                  → "naive believer"

    La misma pipeline se aplica al query del usuario en `gdrive_search.search()`,
    de forma que "jaya" matchea a "Jayā" y viceversa.
    """
    if not name:
        return ""
    n = _STRIP_EXT_RE.sub("", name)
    n = _PAREN_RE.sub(" ", n)      # quita paréntesis, corchetes, llaves
    # Cortar por " - ", " by ", " | ", etc. — nos quedamos solo con la parte previa
    parts = _VARIANT_SPLIT_RE.split(n, maxsplit=1)
    n = parts[0]
    n = _asciifold(n)              # acentos y ligaduras → ASCII
    n = n.lower()
    n = _NONALNUM_RE.sub(" ", n)   # cualquier no-alfanumérico → espacio
    n = _MULTISPACE_RE.sub(" ", n).strip()
    return n


# ---------------------------------------------------------------------------
# Extracción de tags
# ---------------------------------------------------------------------------
# Filosofía: MPC Autofill descarta los `()` y `[]` al normalizar el nombre pero
# los preserva como tags filtrables. Adoptamos el mismo enfoque: extraemos el
# contenido de todos los paréntesis/corchetes del filename y del folder_path,
# los matcheamos contra un vocabulario canónico con aliases, y devolvemos un
# CSV listo para guardar en `IndexedArt.tags`.
#
# El vocabulario está pensado para MTG proxy art:
#   - "Full art"   → full_art
#   - "Borderless" → borderless
#   - "Retro"      → retro
#   - "Textless"   → textless
#   - etc.
#
# Los flags booleanos derivados (is_full_art, is_borderless…) se calculan aquí
# también, para que el indexer los pueda escribir sin lógica duplicada.

# vocabulario canónico: canonical_tag → set de aliases lowercase (sin espacios finales).
# Los aliases se comparan contra el contenido bruto de los brackets, permitiendo
# múltiples formas de nombrar el mismo concepto ("FA" = "full art").
_TAG_VOCABULARY: dict[str, frozenset[str]] = {
    "full_art": frozenset({
        "full art", "fullart", "full-art", "fa",
    }),
    "borderless": frozenset({
        "borderless", "no border", "no-border", "bl",
    }),
    "extended": frozenset({
        "extended", "extended art", "extended-art", "ea",
    }),
    "showcase": frozenset({
        "showcase", "sc",
    }),
    "retro": frozenset({
        "retro", "retro frame", "old border", "old frame", "1993 frame", "old-frame",
    }),
    "textless": frozenset({
        "textless", "no text", "no-text",
    }),
    "promo": frozenset({
        "promo", "pre-release", "prerelease", "pre release", "release",
    }),
    "alt_art": frozenset({
        "alt art", "alt-art", "alternate art", "alternate", "alt",
        "alternative art",
    }),
    "anime": frozenset({
        "anime", "manga",
    }),
    "japanese": frozenset({
        "japanese", "jp", "jpn",
    }),
    "foil": frozenset({
        "foil", "etched", "gilded",
    }),
    "back": frozenset({
        "back", "b",
    }),
}

# Lookup inverso: alias → canonical_tag. Compuesto una vez al import.
_ALIAS_TO_CANONICAL: dict[str, str] = {
    alias: canon
    for canon, aliases in _TAG_VOCABULARY.items()
    for alias in aliases
}

# Regex para extraer contenido de () y []. No queremos ni matchear
# recursivamente ni cruzar entre paréntesis — grupo simple con contenido no-anidado.
_BRACKET_CONTENTS_RE = re.compile(r"[\(\[]([^\(\)\[\]]+)[\)\]]")


def extract_tags(filename: str, folder_path: str = "") -> tuple[str, dict[str, bool]]:
    """Extrae tags canónicos del filename y del folder_path.

    Recorre todos los ``()`` y ``[]`` en ambos, saca el contenido, y lo
    matchea contra el vocabulario canónico. Un mismo tag detectado múltiples
    veces aparece una sola vez en el CSV.

    Devuelve una tupla ``(tags_csv, flags)``:
      - ``tags_csv``: string CSV ordenado alfabéticamente (ej. "borderless,full_art").
      - ``flags``: dict con las claves booleanas is_full_art, is_borderless, etc.
        que se escriben directamente en las columnas de ``IndexedArt``.

    Ejemplos:
      extract_tags("Sol Ring (Full Art).png")
        → ("full_art", {"is_full_art": True, ...})
      extract_tags("Forest (BL) [Retro].png")
        → ("borderless,retro", {"is_borderless": True, "is_retro": True, ...})
      extract_tags("Opt.png", "Anime folder/")
        → ("anime", {"is_anime": True, ...})

    Tag `back` NO se refleja como flag booleano — el indicador de reverso ya
    se maneja en `parse_filename` de custom_art (marker [BACK]). Aquí lo
    detectamos por si un archivo en drive lo lleva, para poder filtrarlo si
    procede, pero no genera un `is_back` (ese contexto pertenece a la lógica
    de front/back de la carta, no al indexado de arte).
    """
    seen: set[str] = set()
    for text in (filename or "", folder_path or ""):
        for content in _BRACKET_CONTENTS_RE.findall(text):
            # Un mismo bracket puede contener varios tags separados por coma:
            # "(FA, Retro)" → ["FA", "Retro"].
            for raw in content.split(","):
                key = _asciifold(raw).lower().strip()
                if not key:
                    continue
                canon = _ALIAS_TO_CANONICAL.get(key)
                if canon:
                    seen.add(canon)

    csv = ",".join(sorted(seen))
    # Flags derivados. Solo los que existen como columna en IndexedArt.
    flags = {
        "is_full_art":   "full_art"   in seen,
        "is_borderless": "borderless" in seen,
        "is_extended":   "extended"   in seen,
        "is_showcase":   "showcase"   in seen,
        "is_retro":      "retro"      in seen,
        "is_textless":   "textless"   in seen,
        "is_promo":      "promo"      in seen,
        "is_alt_art":    "alt_art"    in seen,
    }
    return csv, flags


# ---------------------------------------------------------------------------
# Google Drive API v3 (modo principal, requiere API key)
# ---------------------------------------------------------------------------

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
_PAGE_SIZE = 1000  # máximo permitido por la API
_IMAGE_MIMES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
}
# Fields mínimos que necesitamos por archivo:
_FIELDS = "nextPageToken,files(id,name,mimeType,size,parents,shortcutDetails)"


@dataclass
class IndexResult:
    source_id: int
    files_added: int
    files_updated: int
    folders_visited: int
    error: str | None = None
    used_api_key: bool = False


async def _drive_api_list(
    client: httpx.AsyncClient,
    folder_id: str,
    api_key: str,
    only_images: bool = True,
) -> list[dict]:
    """Lista todos los hijos directos de una carpeta (imágenes y subcarpetas).

    Pagina con nextPageToken hasta agotar. Devuelve lista de dicts con:
    {id, name, mimeType, size?, parents?, shortcutDetails?}
    """
    if only_images:
        q = (
            f"'{folder_id}' in parents and trashed=false and ("
            "mimeType='application/vnd.google-apps.folder' or "
            "mimeType contains 'image/'"
            ")"
        )
    else:
        q = f"'{folder_id}' in parents and trashed=false"

    out: list[dict] = []
    page_token: str | None = None
    while True:
        params = {
            "q": q,
            "pageSize": str(_PAGE_SIZE),
            "fields": _FIELDS,
            "key": api_key,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        r = await client.get(f"{_DRIVE_API_BASE}/files", params=params, timeout=30.0)
        r.raise_for_status()
        payload = r.json()
        out.extend(payload.get("files", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return out


async def _index_via_api(
    db: AsyncSession,
    source: ArtSource,
    folder_id: str,
    api_key: str,
) -> IndexResult:
    """Indexa un drive recursivamente usando la API v3.

    Recorre subcarpetas en BFS (una capa a la vez para no explotar la pila).
    Guarda cada imagen con su ruta relativa desde la raíz.

    Commits parciales cada 500 filas: si el proceso se corta o el usuario
    consulta durante el indexado, ve el progreso real, no todo o nada.
    Log periódico para poder ver el avance en el fichero de log.
    """
    from sqlalchemy import func
    files_added = 0
    files_updated = 0
    folders_visited = 0
    since_last_commit = 0
    _COMMIT_EVERY = 500
    _LOG_FOLDERS_EVERY = 20

    # Cola: (folder_id, path_relativo)
    queue: list[tuple[str, str]] = [(folder_id, "")]
    seen_folders: set[str] = {folder_id}

    async def _partial_commit() -> None:
        """Persiste lo acumulado y actualiza indexed_files para la UI."""
        nonlocal since_last_commit
        source.indexed_files = int(await db.scalar(
            select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source.id)
        ) or 0)
        await db.commit()
        since_last_commit = 0

    async with httpx.AsyncClient(verify=not ssl_insecure()) as client:
        while queue:
            current_id, current_path = queue.pop(0)
            folders_visited += 1

            if folders_visited % _LOG_FOLDERS_EVERY == 0:
                log.info(
                    "  [%s] %d folders visitados, +%d archivos hasta ahora "
                    "(cola: %d folders pendientes)",
                    source.name, folders_visited, files_added, len(queue),
                )

            try:
                items = await _drive_api_list(client, current_id, api_key)
            except httpx.HTTPStatusError as e:
                # Un 403/404 en una subcarpeta no debe abortar todo el drive.
                # Solo abortamos si es en la raíz o es un error de auth.
                if e.response.status_code in (401, 403) and current_id == folder_id:
                    # Persistir lo acumulado antes de salir
                    if since_last_commit > 0:
                        await _partial_commit()
                    return IndexResult(
                        source_id=source.id, files_added=files_added,
                        files_updated=files_updated,
                        folders_visited=folders_visited,
                        error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                        used_api_key=True,
                    )
                log.warning("Saltando subcarpeta %s (%s): %s", current_id, current_path, e)
                continue

            for item in items:
                mime = item.get("mimeType", "")
                item_id = item.get("id")
                name = item.get("name", "")

                # Resolver shortcuts a su target si es un shortcut a un folder o imagen
                if mime == "application/vnd.google-apps.shortcut":
                    sc = item.get("shortcutDetails") or {}
                    target_id = sc.get("targetId")
                    target_mime = sc.get("targetMimeType", "")
                    if not target_id:
                        continue
                    item_id = target_id
                    mime = target_mime

                if mime == "application/vnd.google-apps.folder":
                    if item_id and item_id not in seen_folders:
                        seen_folders.add(item_id)
                        subpath = f"{current_path}/{name}" if current_path else name
                        queue.append((item_id, subpath))
                    continue

                if mime not in _IMAGE_MIMES:
                    continue

                # Upsert manual (buscar por source_id+file_id)
                existing = (await db.execute(
                    select(IndexedArt).where(
                        IndexedArt.source_id == source.id,
                        IndexedArt.file_id == item_id,
                    )
                )).scalar_one_or_none()

                size = int(item.get("size", 0) or 0)
                tags_csv, tag_flags = extract_tags(name, current_path)
                if existing:
                    existing.filename = name
                    existing.name_normalized = normalize_filename(name)
                    existing.folder_path = current_path
                    existing.size_bytes = size
                    existing.mime_type = mime
                    existing.indexed_at = datetime.now(timezone.utc)
                    existing.tags = tags_csv
                    for flag, value in tag_flags.items():
                        setattr(existing, flag, value)
                    files_updated += 1
                else:
                    db.add(IndexedArt(
                        source_id=source.id,
                        file_id=item_id,
                        filename=name,
                        name_normalized=normalize_filename(name),
                        folder_path=current_path,
                        size_bytes=size,
                        mime_type=mime,
                        tags=tags_csv,
                        **tag_flags,
                    ))
                    files_added += 1
                since_last_commit += 1

                # Commit parcial: la UI ve progreso y no perdemos datos si
                # algo va mal.
                if since_last_commit >= _COMMIT_EVERY:
                    await _partial_commit()

    # Commit final del residuo
    if since_last_commit > 0:
        await _partial_commit()

    return IndexResult(
        source_id=source.id, files_added=files_added, files_updated=files_updated,
        folders_visited=folders_visited, used_api_key=True,
    )


# ---------------------------------------------------------------------------
# Fallback: scraping de embeddedfolderview
# ---------------------------------------------------------------------------

# El HTML de embeddedfolderview incluye scripts con datos como:
# {"data":[["FILE_ID","file",...,"NAME",...]]} — se puede regexear.
# Es frágil pero funciona hoy (comprobado). Solo devuelve el primer nivel.
_EMBED_ITEM_RE = re.compile(
    r'"([A-Za-z0-9_\-]{20,})"[^"]*?"application/[^"]+/([^"]+)"[^"]*?"([^"]+\.(?:png|jpe?g|webp|gif))"',
    re.IGNORECASE,
)


async def _index_via_scraping(
    db: AsyncSession,
    source: ArtSource,
    folder_id: str,
) -> IndexResult:
    """Modo pobre sin API key. Solo indexa el primer nivel (sin subcarpetas)
    y sin tamaño/mime fiable. Advierte al usuario en el error message si
    detectamos que el drive es muy grande.
    """
    url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
    files_added = 0
    files_updated = 0

    async with httpx.AsyncClient(verify=not ssl_insecure(), follow_redirects=True) as client:
        try:
            r = await client.get(url, timeout=30.0, headers={
                "User-Agent": "Mozilla/5.0 (compatible; MPC-Forge indexer)",
            })
            r.raise_for_status()
            html = r.text
        except (httpx.HTTPError, httpx.HTTPStatusError) as e:
            return IndexResult(
                source_id=source.id, files_added=0, files_updated=0,
                folders_visited=0,
                error=f"No se pudo cargar embedded view: {e}",
            )

    # Parseamos con regex simple
    matches = _EMBED_ITEM_RE.findall(html)
    for file_id, mime_frag, name in matches:
        # Filtro solo imágenes
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            continue
        mime = f"image/{mime_frag.split('/')[-1]}"
        tags_csv, tag_flags = extract_tags(name, "")
        existing = (await db.execute(
            select(IndexedArt).where(
                IndexedArt.source_id == source.id,
                IndexedArt.file_id == file_id,
            )
        )).scalar_one_or_none()
        if existing:
            existing.filename = name
            existing.name_normalized = normalize_filename(name)
            existing.mime_type = mime
            existing.indexed_at = datetime.now(timezone.utc)
            existing.tags = tags_csv
            for flag, value in tag_flags.items():
                setattr(existing, flag, value)
            files_updated += 1
        else:
            db.add(IndexedArt(
                source_id=source.id, file_id=file_id, filename=name,
                name_normalized=normalize_filename(name),
                folder_path="", size_bytes=0, mime_type=mime,
                tags=tags_csv,
                **tag_flags,
            ))
            files_added += 1

    err = None
    if files_added + files_updated == 0:
        err = (
            "No se encontraron imágenes en la vista pública. "
            "Puede ser que el drive tenga estructura profunda (sin API key solo "
            "indexamos el primer nivel), o que la carpeta ya no sea pública."
        )
    return IndexResult(
        source_id=source.id, files_added=files_added, files_updated=files_updated,
        folders_visited=1, error=err,
    )


# ---------------------------------------------------------------------------
# API pública del módulo
# ---------------------------------------------------------------------------

_FOLDER_ID_RE = re.compile(r"folders/([A-Za-z0-9_\-]+)")


def _extract_folder_id(url: str) -> str | None:
    m = _FOLDER_ID_RE.search(url)
    return m.group(1) if m else None


async def index_source(db: AsyncSession, source_id: int) -> IndexResult:
    """Indexa un drive. Usa API key si está configurada, si no cae a scraping.

    Serializa vía semáforo global: aunque la UI lance N indexados en paralelo,
    se ejecutan uno a uno para no saturar el lock de SQLite.

    Es una operación potencialmente larga (segundos a minutos para drives
    grandes). Debe llamarse desde una BackgroundTask, no bloqueando la request.
    """
    source = await db.get(ArtSource, source_id)
    if not source:
        return IndexResult(source_id=source_id, files_added=0, files_updated=0,
                          folders_visited=0, error="Source no encontrado")

    folder_id = _extract_folder_id(source.url)
    if not folder_id:
        result = IndexResult(
            source_id=source_id, files_added=0, files_updated=0,
            folders_visited=0,
            error="La URL no parece un folder de Google Drive",
        )
        source.indexed_at = datetime.now(timezone.utc)
        source.index_error = result.error or ""
        await db.commit()
        return result

    async with _INDEX_SEMAPHORE:  # máx 3 concurrentes
        api_key = (getattr(cfg, "GOOGLE_API_KEY", "") or "").strip()
        mode = "API v3" if api_key else "scraping (sin API key)"
        log.info("▶ Empezando indexado de source %d (%s) vía %s",
                 source_id, source.name, mode)
        if api_key:
            result = await _index_via_api(db, source, folder_id, api_key)
        else:
            result = await _index_via_scraping(db, source, folder_id)

        # Actualizar estado del source. Contamos filas reales de IndexedArt.
        from sqlalchemy import func
        source.indexed_at = datetime.now(timezone.utc)
        source.indexed_files = int(await db.scalar(
            select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source.id)
        ) or 0)
        source.index_error = result.error or ""
        await db.commit()

    log.info(
        "✓ Terminado source %d (%s): total=%d archivos (+%d nuevos, ~%d actualizados) "
        "en %d folders. Error=%s",
        source_id, source.name, source.indexed_files, result.files_added,
        result.files_updated, result.folders_visited, result.error or "ninguno",
    )
    return result


async def clear_index(db: AsyncSession, source_id: int) -> int:
    """Borra todo el índice de un source. Devuelve nº de filas borradas."""
    from sqlalchemy import func
    n = int(await db.scalar(
        select(func.count(IndexedArt.id)).where(IndexedArt.source_id == source_id)
    ) or 0)
    await db.execute(delete(IndexedArt).where(IndexedArt.source_id == source_id))
    source = await db.get(ArtSource, source_id)
    if source:
        source.indexed_at = None
        source.indexed_files = 0
        source.index_error = ""
    await db.commit()
    return n


# ---------------------------------------------------------------------------
# Backfill de nombres normalizados
# ---------------------------------------------------------------------------

_NORMALIZATION_VERSION_KEY = "gdrive.normalization_version"


async def backfill_normalized_names(db: AsyncSession) -> int:
    """Recalcula ``name_normalized`` y ``tags``/flags si la versión cambió.

    Se ejecuta al arrancar (desde ``lifespan`` en ``app.py``). Compara la
    versión guardada en ``KeyValue`` con ``NORMALIZATION_VERSION``. Si difieren
    (o si nunca se ejecutó), reprocesa TODAS las filas en batches de 1000 y
    actualiza en su sitio, sin borrar el índice ni exigir al usuario reindexar.

    Idempotente: ejecutarlo dos veces no cambia nada si la versión está al día.

    Qué se actualiza:
      - ``name_normalized``: aplica la pipeline actual (con asciifolding
        desde v2).
      - ``tags`` y flags booleanos (``is_full_art``, ``is_borderless``, …):
        extraídos del filename y del folder_path (desde v3).

    Rendimiento: para 500k filas, ~5 segundos. Corremos en background dentro
    del lifespan para no bloquear el arranque de la UI.

    Devuelve el número de filas actualizadas (0 si no había cambio o índice
    vacío).
    """
    from mpc_forge.models import KeyValue

    # ¿Ya está en la versión actual?
    kv = await db.get(KeyValue, _NORMALIZATION_VERSION_KEY)
    try:
        current = int(kv.value) if kv else 0
    except (ValueError, AttributeError):
        current = 0
    if current >= NORMALIZATION_VERSION:
        return 0

    total = int(await db.scalar(
        select(__import__("sqlalchemy").func.count(IndexedArt.id))
    ) or 0)
    if total == 0:
        # Índice vacío — marcamos la versión y salimos.
        if kv:
            kv.value = str(NORMALIZATION_VERSION)
        else:
            db.add(KeyValue(key=_NORMALIZATION_VERSION_KEY, value=str(NORMALIZATION_VERSION)))
        await db.commit()
        return 0

    log.info(
        "Backfill de normalización: reprocesando %d filas (v%d → v%d)…",
        total, current, NORMALIZATION_VERSION,
    )
    updated = 0
    batch_size = 1000
    offset = 0
    while offset < total:
        rows = (await db.scalars(
            select(IndexedArt)
            .order_by(IndexedArt.id)
            .offset(offset)
            .limit(batch_size)
        )).all()
        if not rows:
            break
        for art in rows:
            new_norm = normalize_filename(art.filename)
            new_tags_csv, new_flags = extract_tags(art.filename, art.folder_path)
            row_changed = False
            if new_norm != art.name_normalized:
                art.name_normalized = new_norm
                row_changed = True
            if new_tags_csv != (art.tags or ""):
                art.tags = new_tags_csv
                row_changed = True
            for flag, value in new_flags.items():
                if getattr(art, flag, False) != value:
                    setattr(art, flag, value)
                    row_changed = True
            if row_changed:
                updated += 1
        await db.commit()
        offset += batch_size

    # Registramos la versión completada.
    kv = await db.get(KeyValue, _NORMALIZATION_VERSION_KEY)
    if kv:
        kv.value = str(NORMALIZATION_VERSION)
    else:
        db.add(KeyValue(key=_NORMALIZATION_VERSION_KEY, value=str(NORMALIZATION_VERSION)))
    await db.commit()
    log.info("Backfill de normalización completado: %d filas actualizadas de %d totales",
             updated, total)
    return updated
