"""Cache de pares double-faced (DFC + meld) descargado de Scryfall.

Objetivo
--------
Cuando el usuario importa un mazo (Moxfield, texto plano, o de un URL de
otro sitio), muchas cartas necesitan que sepamos su reverso: los DFC como
Delver of Secrets, MDFCs (Zendikar Rising+) y meld pieces (Bruna/Gisela →
Brisela). Consultarlo por carta a Scryfall es caro; precomputamos la lista
completa una vez y la mantenemos fresca cada 7 días.

Estrategia
----------
1. Sync inicial al arrancar (si tabla vacía o han pasado >7 días).
2. Fuente: Scryfall search API con ``is:dfc`` e ``is:meld``.
3. Guardamos en la tabla ``dfc_pairs`` con UNIQUE en front_name — idempotente.
4. El resolver de decks consulta primero esta tabla (síncrono, memoria local)
   y solo cae a Scryfall si no hay match.

Rendimiento
-----------
- Sync completo: ~1500 pares DFC + ~15 meld pairs = ~1520 filas. Tarda ~10s.
- Consulta local: O(log N) por WHERE indexado.
- Compatible offline una vez sembrado.

Referencia
----------
MPC Autofill hace exactamente lo mismo en su ``MTGIntegration.get_dfc_pairs``.
Adaptamos aquí a nuestro cliente `ScryfallClient` y a nuestra BD.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import DFCPair, KeyValue

log = logging.getLogger(__name__)

# Cada cuánto refrescamos desde Scryfall. Balance entre frescura (nuevos sets
# salen cada ~3 meses) y no molestar a Scryfall. Una semana es más que suficiente.
SYNC_TTL = timedelta(days=7)

# Timestamp de la última sync guardado en KeyValue para no depender del min(fetched_at).
_LAST_SYNC_KEY = "dfc_pairs.last_synced_at"

# Queries Scryfall — los mismos que usa MPC Autofill.
_DFC_QUERY = "is:dfc -layout:art_series -(layout:double_faced_token -keyword:transform) -is:reversible"
_MELD_QUERY = "is:meld"


async def _fetch_paginated(scryfall: ScryfallClient, query: str) -> list[dict[str, Any]]:
    """Recorre todas las páginas del search API para el query dado.

    Scryfall pagina de 175 en 175. Un ``is:dfc`` devuelve ~1500 resultados.
    La paginación pasa por el rate limiter del cliente (``/cards/search`` es un
    endpoint de 2 peticiones/s).
    """
    return await scryfall.search_all(query, unique="cards")


def _dfc_pair_from_card(card: dict[str, Any]) -> DFCPair | None:
    """Extrae front/back para una carta transform o modal_dfc (no meld)."""
    faces = card.get("card_faces") or []
    if len(faces) < 2:
        return None
    front = faces[0].get("name")
    back = faces[1].get("name")
    if not front or not back:
        return None
    layout = card.get("layout", "transform")
    if layout not in {"transform", "modal_dfc"}:
        # Otras layouts (art_series, reversible_card…) ya excluidos por query,
        # pero por si acaso las descartamos.
        return None
    return DFCPair(
        front_name=front,
        back_name=back,
        kind=layout,
    )


def _meld_pairs_from_card(card: dict[str, Any]) -> list[DFCPair]:
    """Extrae las relaciones meld_part → meld_result para una carta meld.

    Un meld tiene 3 partes en all_parts: dos meld_parts (top/bottom) y un
    meld_result (Brisela, Chittering Host, etc). Por cada meld_part que sea
    esta carta, generamos una fila apuntando al meld_result como "back".

    ``kind`` distingue meld_top/meld_bottom mirando el oracle text:
    la mitad "de abajo" es la que dice "Melds with X".
    """
    all_parts = card.get("all_parts") or []
    if not all_parts:
        return []
    self_id = card.get("id")
    self_name = card.get("name")

    # Buscar el meld_result entre las partes relacionadas
    meld_result = next(
        (p for p in all_parts if p.get("component") == "meld_result"),
        None,
    )
    if not meld_result:
        return []
    result_name = meld_result.get("name")
    if not result_name:
        return []

    # ¿Esta carta es una meld_part? (En Scryfall, la carta se lista a sí misma en all_parts)
    is_self_meld_part = any(
        p.get("id") == self_id and p.get("component") == "meld_part"
        for p in all_parts
    )
    if not is_self_meld_part:
        return []

    # Determinar si es top o bottom por el oracle text (heurística MPC Autofill).
    # Las cartas "top" no mencionan "Melds with X" en su oracle. Las "bottom" sí.
    oracle = card.get("oracle_text", "") or ""
    is_top = "\n(Melds with " not in oracle and "Melds with " not in oracle

    kind = "meld_top" if is_top else "meld_bottom"
    bit = "Top" if is_top else "Bottom"
    return [DFCPair(
        front_name=self_name,
        back_name=f"{result_name} {bit}",
        kind=kind,
    )]


async def _fetch_all_pairs(scryfall: ScryfallClient) -> list[DFCPair]:
    """Descarga todos los pares desde Scryfall.

    Devuelve una lista de instancias DFCPair (no persistidas todavía).
    Los duplicados por front_name se resuelven mantendo la primera aparición
    — Scryfall devuelve una entrada por printing, pero solo nos interesa el
    par de nombres (que no cambia entre impresiones).
    """
    pairs: dict[str, DFCPair] = {}

    # 1) DFCs regulares (transform + modal_dfc)
    dfc_cards = await _fetch_paginated(scryfall, _DFC_QUERY)
    for card in dfc_cards:
        if card.get("digital"):
            continue
        pair = _dfc_pair_from_card(card)
        if pair and pair.front_name not in pairs:
            pairs[pair.front_name] = pair

    # 2) Meld pieces
    meld_cards = await _fetch_paginated(scryfall, _MELD_QUERY)
    for card in meld_cards:
        if card.get("digital"):
            continue
        for pair in _meld_pairs_from_card(card):
            if pair.front_name not in pairs:
                pairs[pair.front_name] = pair

    return list(pairs.values())


async def _mark_synced(db: AsyncSession) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    if kv:
        kv.value = now_iso
    else:
        db.add(KeyValue(key=_LAST_SYNC_KEY, value=now_iso))


async def _is_stale(db: AsyncSession) -> bool:
    """True si la tabla está vacía o han pasado >7 días desde la última sync."""
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    if not kv or not kv.value:
        # Nunca se ha sincronizado — hay que hacerlo.
        return True
    try:
        last = datetime.fromisoformat(kv.value)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) > SYNC_TTL


async def _count(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count(DFCPair.id))) or 0)


async def sync_if_stale(db: AsyncSession, scryfall: ScryfallClient) -> dict[str, Any]:
    """Refresca la tabla desde Scryfall si TTL expirado o tabla vacía.

    Idempotente y seguro para llamar en cada arranque: si acaba de sincronizarse,
    no hace ninguna llamada de red.

    Estrategia de reemplazo: recreamos toda la tabla en una transacción para
    evitar estados intermedios donde falten pares. Si Scryfall falla, dejamos
    la tabla anterior intacta.
    """
    stale = await _is_stale(db)
    existing_count = await _count(db)
    if not stale and existing_count > 0:
        return {"synced": False, "pairs": existing_count, "reason": "up_to_date"}

    log.info("Sincronizando DFC pairs desde Scryfall (existentes=%d, stale=%s)…",
             existing_count, stale)
    try:
        pairs = await _fetch_all_pairs(scryfall)
    except Exception as e:  # noqa: BLE001
        # Si Scryfall no responde y ya tenemos datos, mantenemos los actuales.
        if existing_count > 0:
            log.warning("Fallo al sincronizar DFC pairs pero la tabla actual sigue viva: %s", e)
            return {"synced": False, "pairs": existing_count, "reason": f"error: {e!s}"}
        # Si la tabla está vacía y falla, propagar (el caller decide qué hacer).
        raise

    # Reemplazo transaccional: borramos e insertamos en el mismo commit.
    await db.execute(delete(DFCPair))
    db.add_all(pairs)
    await _mark_synced(db)
    await db.commit()

    log.info("DFC pairs sincronizados: %d pares (%d DFCs regulares, %d meld pieces)",
             len(pairs),
             sum(1 for p in pairs if p.kind in {"transform", "modal_dfc"}),
             sum(1 for p in pairs if p.kind.startswith("meld_")))
    return {"synced": True, "pairs": len(pairs), "reason": "refreshed"}


async def get_back_name(db: AsyncSession, front_name: str) -> str | None:
    """Devuelve el nombre del reverso para una carta dada, o None.

    Consulta local en la tabla ``dfc_pairs``. Case-insensitive.
    Uso principal: al importar por texto plano, saber qué reverso añadir sin
    tener que llamar a Scryfall.
    """
    if not front_name:
        return None
    # LOWER() en ambos lados para case-insensitive. La tabla es pequeña
    # (~1500 filas) así que el escaneo con LOWER es aceptable, y evitamos
    # duplicar datos con un name_lower indexado.
    row = await db.scalar(
        select(DFCPair).where(
            func.lower(DFCPair.front_name) == front_name.lower()
        )
    )
    return row.back_name if row else None


async def bulk_lookup(db: AsyncSession, names: list[str]) -> dict[str, dict[str, str]]:
    """Lookup masivo de nombres → ``{"back_name": ..., "kind": ...}`` desde cache.

    Uso: previews de import ("¿cuántas cartas de mi decklist son DFC?"),
    analytics, y cualquier UI que necesite saber qué cartas son doble-cara
    SIN hacer round-trip a Scryfall.

    - Un solo `SELECT WHERE lower(front_name) IN (…)` — O(N) filas
      escaneadas gracias al índice ``ix_dfc_pairs_front_lower``.
    - Case-insensitive: el mapa de salida usa el nombre EXACTO que el
      caller pidió (útil para reconciliar con el input original).

    Devuelve solo las cartas que están en el cache. Las que no están en el
    cache se omiten (no significa que no sean DFC — puede ser que el cache
    esté desactualizado; el caller decide qué hacer).
    """
    if not names:
        return {}
    # Normalizar a lower para el WHERE IN.
    lowered = [n.lower() for n in names if n]
    rows = (await db.execute(
        select(DFCPair.front_name, DFCPair.back_name, DFCPair.kind)
        .where(func.lower(DFCPair.front_name).in_(lowered))
    )).all()
    # Índice: lower(front_name) → (back_name, kind)
    by_lower: dict[str, tuple[str, str]] = {
        f.lower(): (b, k) for f, b, k in rows
    }
    # Emit devolvemos los nombres EXACTOS del caller (preservando su casing)
    # para que la reconciliación con el decklist original sea trivial.
    out: dict[str, dict[str, str]] = {}
    for n in names:
        if not n:
            continue
        found = by_lower.get(n.lower())
        if found:
            out[n] = {"back_name": found[0], "kind": found[1]}
    return out


async def get_all_pairs(db: AsyncSession) -> list[tuple[str, str, str]]:
    """Devuelve todos los pares como tuplas (front, back, kind). Útil para
    exponer via API o para pre-cargar en memoria un dict rápido.
    """
    rows = (await db.execute(
        select(DFCPair.front_name, DFCPair.back_name, DFCPair.kind)
    )).all()
    return [(f, b, k) for f, b, k in rows]


async def stats(db: AsyncSession) -> dict[str, Any]:
    """Info para la UI de Ajustes / debug."""
    total = await _count(db)
    kv = await db.get(KeyValue, _LAST_SYNC_KEY)
    last = kv.value if kv else None
    return {
        "total_pairs": total,
        "last_synced_at": last,
    }
