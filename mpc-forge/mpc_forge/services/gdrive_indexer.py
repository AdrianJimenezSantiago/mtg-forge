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
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)


# Semáforo global: máximo 1 indexado concurrente.
# SQLite serializa escrituras y con 67 tareas escribiendo miles de filas se
# saturaría el busy_timeout de 30s. Con concurrencia=1 nunca hay conflicto y
# los indexados son igual de rápidos porque la API v3 es más lenta que SQLite.
_INDEX_SEMAPHORE = asyncio.Semaphore(1)


# ---------------------------------------------------------------------------
# Nombres normalizados para fuzzy search
# ---------------------------------------------------------------------------

_STRIP_EXT_RE = re.compile(r"\.(png|jpe?g|webp|gif)$", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*[\[\(\{].*?[\]\)\}]\s*")   # elimina "(Anime)" "[BACK]" etc.
_NONALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_MULTISPACE_RE = re.compile(r"\s+")


def normalize_filename(name: str) -> str:
    """Convierte "Sol Ring (Daubrez Borderless).png" → "sol ring".

    Nos quedamos con la parte "canónica" del nombre para el índice principal.
    En búsqueda seguiremos comparando también contra el nombre completo para
    respetar variantes.
    """
    if not name:
        return ""
    n = _STRIP_EXT_RE.sub("", name)
    n = _PAREN_RE.sub(" ", n)      # quita paréntesis, corchetes, llaves
    n = n.lower()
    n = _NONALNUM_RE.sub(" ", n)   # cualquier no-alfanumérico → espacio
    n = _MULTISPACE_RE.sub(" ", n).strip()
    return n


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
    """
    files_added = 0
    files_updated = 0
    folders_visited = 0
    # Cola: (folder_id, path_relativo)
    queue: list[tuple[str, str]] = [(folder_id, "")]
    seen_folders: set[str] = {folder_id}

    async with httpx.AsyncClient(verify=not ssl_insecure()) as client:
        while queue:
            current_id, current_path = queue.pop(0)
            folders_visited += 1
            try:
                items = await _drive_api_list(client, current_id, api_key)
            except httpx.HTTPStatusError as e:
                # Un 403/404 en una subcarpeta no debe abortar todo el drive.
                # Solo abortamos si es en la raíz o es un error de auth.
                if e.response.status_code in (401, 403) and current_id == folder_id:
                    return IndexResult(
                        source_id=source.id, files_added=0, files_updated=0,
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
                if existing:
                    existing.filename = name
                    existing.name_normalized = normalize_filename(name)
                    existing.folder_path = current_path
                    existing.size_bytes = size
                    existing.mime_type = mime
                    existing.indexed_at = datetime.now(timezone.utc)
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
                    ))
                    files_added += 1

            # Flush periódico para no acumular demasiado en memoria
            if (files_added + files_updated) % 500 == 0:
                await db.flush()

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
            files_updated += 1
        else:
            db.add(IndexedArt(
                source_id=source.id, file_id=file_id, filename=name,
                name_normalized=normalize_filename(name),
                folder_path="", size_bytes=0, mime_type=mime,
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

    async with _INDEX_SEMAPHORE:  # solo un indexado a la vez
        api_key = (getattr(cfg, "GOOGLE_API_KEY", "") or "").strip()
        if api_key:
            log.info("Indexando source %d (%s) vía API v3", source_id, source.name)
            result = await _index_via_api(db, source, folder_id, api_key)
        else:
            log.info("Indexando source %d (%s) vía scraping (sin API key)",
                     source_id, source.name)
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
        "Indexado source %d: +%d, ~%d, %d folders visitados (err=%s)",
        source_id, result.files_added, result.files_updated,
        result.folders_visited, result.error,
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
