"""Validación y enriquecimiento de metadatos canónicos con Scryfall.

Motivación (Extras · F2/T5)
---------------------------
`gdrive_indexer.extract_canonical` captura `[SET NUM]` del filename sin
validar que ese (set, num) exista realmente. Un usuario que escribe
"[XYZ 999]" acaba con un canonical inválido en la BD.

Este servicio:
  1. Recorre los IndexedArt con canonical no verificado.
  2. Los agrupa por (set, num) — muchos artes comparten la misma printing
     canónica, así una sola query Scryfall resuelve N filas.
  3. Consulta Scryfall vía `by_set_and_number`.
  4. Si la impresión existe, popula `oracle_id`/`artist` en filas
     relacionadas (via nuevas columnas o via lookup fresh cada uso).
     Si NO existe, borra el canonical (limpieza defensiva).

Trade-offs
----------
- Se ejecuta bajo demanda por el usuario (`POST /api/drives/canonical/validate`),
  no automáticamente al indexar. Batch de N canonicals → ceil(N/75) requests
  a Scryfall (que soporta hasta 75 identifiers por `/cards/collection`).
- Los resultados enriquecidos se cachean en `PrintingCache` — la próxima
  vez que el usuario abra el picker, el arte del drive puede mostrar
  "art by Rebecca Guay" sin llamar a Scryfall.

TODO fuera de este servicio: exponer `oracle_id`/`artist` como columnas
directas en `IndexedArt` para filtros SQL eficientes. Por ahora vive en
`PrintingCache` (via scryfall_id) — el frontend hace un JOIN implícito.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import IndexedArt, PrintingCache

log = logging.getLogger(__name__)

# Scryfall permite hasta 75 identifiers por /cards/collection request.
_SCRYFALL_BATCH_SIZE = 75


async def validate_and_enrich(
    db: AsyncSession,
    scryfall: ScryfallClient,
    limit: int = 500,
    source_id: int | None = None,
) -> dict[str, int]:
    """Valida los `expansion_code + collector_number` no verificados.

    ``limit`` cap de artes a validar en una llamada (evita agotar Scryfall
    con drives gigantes). Batches de 75 en 75.

    ``source_id`` opcional para restringir la validación a un solo drive.

    Devuelve stats::
      {"checked": N, "valid": M, "invalid": K, "cache_hits": H}

    - "valid": la (set, num) existe en Scryfall. Se hidrata `PrintingCache`.
    - "invalid": no existe. Limpiamos `expansion_code`/`collector_number`
      del arte para no ensuciar el picker.
    - "cache_hits": ya teníamos la printing en `PrintingCache` — no hubo
      llamada de red.
    """
    # 1) Recolectar artes con canonical no vacío
    stmt = (
        select(IndexedArt)
        .where(IndexedArt.expansion_code.is_not(None))
        .where(IndexedArt.collector_number.is_not(None))
        .limit(limit)
    )
    if source_id is not None:
        stmt = stmt.where(IndexedArt.source_id == source_id)
    arts = (await db.scalars(stmt)).all()
    if not arts:
        return {"checked": 0, "valid": 0, "invalid": 0, "cache_hits": 0}

    # 2) Deduplicar por (set, num) — muchos artes = misma impresión canónica
    by_key: dict[tuple[str, str], list[IndexedArt]] = {}
    for a in arts:
        key = (a.expansion_code, a.collector_number)
        by_key.setdefault(key, []).append(a)

    # 3) Chequear cache local en un único query. `tuple_.in_` genera
    # ``WHERE (set_code, collector_number) IN ((?, ?), …)`` — SQLite lo
    # optimiza si hay índice combinado; si no, escanea una sola vez. Antes:
    # una SELECT por clave (N queries).
    from sqlalchemy import tuple_
    resolved: dict[tuple[str, str], PrintingCache] = {}
    keys = list(by_key.keys())
    if keys:
        cache_rows = (await db.scalars(
            select(PrintingCache).where(
                tuple_(PrintingCache.set_code, PrintingCache.collector_number).in_(keys)
            )
        )).all()
        for pc in cache_rows:
            k = (pc.set_code, pc.collector_number)
            if k in by_key and k not in resolved:
                resolved[k] = pc
    cache_hits = len(resolved)
    to_query: list[tuple[str, str]] = [k for k in keys if k not in resolved]

    # 4) Batches a Scryfall /cards/collection para las (set, num) faltantes
    valid = 0
    invalid = 0
    for i in range(0, len(to_query), _SCRYFALL_BATCH_SIZE):
        batch = to_query[i:i + _SCRYFALL_BATCH_SIZE]
        idents = [
            {"set": s, "collector_number": n}
            for s, n in batch
        ]
        try:
            cards = await scryfall.collection(idents)
        except Exception as e:
            log.warning("Scryfall.collection batch falló, saltando: %s", e)
            continue
        # Los results de Scryfall vienen sin garantía de orden. Reindexar por (set, num).
        from mpc_forge.services.deck_service import upsert_printings
        cached = await upsert_printings(db, cards)
        found_keys: set[tuple[str, str]] = set()
        for c in cards:
            key = (
                (c.get("set") or "").lower(),
                c.get("collector_number") or "",
            )
            found_keys.add(key)
            resolved[key] = cached[c["id"]]
            valid += 1
        # Marca los que NO aparecieron en la respuesta como inválidos
        for key in batch:
            if key not in found_keys:
                invalid += 1
                for art in by_key.get(key, []):
                    art.expansion_code = None
                    art.collector_number = None
                    art.canonical_source = "invalid"
    await db.commit()

    # 5) Sumar los cache hits al total válido (para stats coherentes)
    valid += cache_hits

    return {
        "checked": len(arts),
        "valid": valid,
        "invalid": invalid,
        "cache_hits": cache_hits,
    }


async def get_canonical_details(
    db: AsyncSession, expansion_code: str, collector_number: str
) -> dict[str, Any] | None:
    """Lookup local de detalles enriquecidos (artist, oracle_id, ...) para
    una (set, num). Devuelve None si no está en cache.

    Uso: el frontend puede llamarlo para mostrar "art by X" bajo un
    thumbnail de drive con canonical.
    """
    pc = await db.scalar(
        select(PrintingCache)
        .where(PrintingCache.set_code == expansion_code.lower())
        .where(PrintingCache.collector_number == collector_number)
        .limit(1)
    )
    if not pc:
        return None
    return {
        "scryfall_id": pc.scryfall_id,
        "oracle_id": pc.oracle_id,
        "name": pc.name,
        "artist": pc.artist,
        "set_code": pc.set_code,
        "set_name": pc.set_name,
        "collector_number": pc.collector_number,
        "released_at": pc.released_at,
    }
