"""Análisis de tokens del mazo y adición masiva de partes relacionadas.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    Depends,
    HTTPException,
    status,
)
from pydantic import BaseModel
from sqlalchemy import select

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import (
    DeckCard,
    PrintingCache,
)
from mpc_forge.schemas import (
    DeckCardView,
)
from mpc_forge.services import (
    deck_activity,
    deck_service,
)
from mpc_forge.services.deck_activity import DeckActivityKind as K

log = logging.getLogger(__name__)


# --- Import / CRUD -------------------------------------------------------

from mpc_forge.routes.decks._common import (
    DbDep,
    _get_scryfall,
    make_router,
)
from mpc_forge.routes.decks._views import (
    _deckcards_to_views,
)

router = make_router()


# ANÁLISIS DE TOKENS DEL MAZO
# ================================================================

@router.get("/{deck_id}/tokens-analysis")
async def tokens_analysis(
    deck_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> dict:
    """Devuelve todos los tokens únicos que las cartas del mazo generan.

    Consolida los `related_parts` (filtrando SOLO `component=='token'`) de
    las cartas activas del mazo (con include=True), dedupe por scryfall_id,
    y para cada token indica:
      - metadata (nombre, tipo, colores, imagen)
      - qué carta(s) del mazo lo genera
      - si ya está en el mazo (con role='tokens')

    Los meld parts/results NO se incluyen aquí: van por el botón individual
    de cada carta porque son específicos y no consolidables.
    """
    import json as _json

    # Cartas activas del mazo (excluyendo tokens/meld_result — los generadores
    # son commander/mainboard/sideboard, no queremos que los tokens generen tokens
    # de sí mismos si por accidente tuvieran related_parts).
    generator_roles = {"commander", "mainboard", "sideboard"}
    cards = (
        await db.scalars(
            select(DeckCard).where(
                DeckCard.deck_id == deck_id,
                DeckCard.include.is_(True),
                DeckCard.role.in_(generator_roles),
            )
        )
    ).all()

    if not cards:
        return {"tokens": [], "total_unique": 0, "already_in_deck": 0, "missing": 0}

    # BATCH: printings de todos los generadores (para leer related_parts)
    scryfall_ids = {c.scryfall_id for c in cards}
    printings = (
        await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids))
        )
    ).all()
    printings_by_id = {p.scryfall_id: p for p in printings}

    # Recolectar tokens únicos y quién los genera
    # tokens_map[scryfall_id_del_token] = {
    #   "name": str, "generated_by": [{deck_card_id, name, quantity}, ...]
    # }
    tokens_map: dict[str, dict] = {}
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        if not printing or not printing.related_parts:
            continue
        try:
            related = _json.loads(printing.related_parts)
        except (ValueError, TypeError):
            continue
        for part in related:
            if part.get("component") != "token":
                continue
            token_sfid = part.get("id")
            if not token_sfid:
                continue
            entry = tokens_map.setdefault(token_sfid, {
                "name": part.get("name") or "Token",
                "generated_by": [],
            })
            entry["generated_by"].append({
                "deck_card_id": dc.id,
                "name": dc.name,
                "quantity": dc.quantity,
            })

    if not tokens_map:
        return {"tokens": [], "total_unique": 0, "already_in_deck": 0, "missing": 0}

    # BATCH: metadata de todos los tokens desde cache local (imagen, tipo, etc.)
    token_sfids = list(tokens_map.keys())
    token_printings = (
        await db.scalars(
            select(PrintingCache).where(PrintingCache.scryfall_id.in_(token_sfids))
        )
    ).all()
    token_meta_by_id = {p.scryfall_id: p for p in token_printings}

    # BATCH: qué tokens ya están en el mazo (por scryfall_id)
    already_in_deck_rows = (
        await db.execute(
            select(DeckCard.id, DeckCard.scryfall_id, DeckCard.quantity)
            .where(
                DeckCard.deck_id == deck_id,
                DeckCard.scryfall_id.in_(token_sfids),
            )
        )
    ).all()
    in_deck_by_sfid = {sfid: (dc_id, qty) for dc_id, sfid, qty in already_in_deck_rows}

    # Para tokens sin metadata cacheada, la pedimos a Scryfall (uno por uno con
    # el rate limit de ScryfallClient). Suele ser rápido porque son pocos por mazo.
    missing_meta = [s for s in token_sfids if s not in token_meta_by_id]
    for sfid in missing_meta:
        try:
            raw = await scryfall.by_id(sfid)
            if raw:
                cached = await deck_service.upsert_printing(db, raw)
                token_meta_by_id[sfid] = cached
        except Exception as e:
            log.warning("No se pudo cachear metadata de token %s: %s", sfid, e)
    if missing_meta:
        await db.commit()

    # Ensamblar respuesta
    tokens_out = []
    already_count = 0
    for sfid, info in tokens_map.items():
        meta = token_meta_by_id.get(sfid)
        deck_card_id, qty_in_deck = in_deck_by_sfid.get(sfid, (None, 0))
        in_deck = deck_card_id is not None
        if in_deck:
            already_count += 1
        tokens_out.append({
            "scryfall_id": sfid,
            "name": (meta.name if meta else info["name"]) or "Token",
            "type_line": meta.type_line if meta else "",
            "colors": meta.colors.split(",") if (meta and meta.colors) else [],
            "image_url": meta.image_normal if meta else None,
            "set_code": meta.set_code if meta else "",
            "in_deck": in_deck,
            "deck_card_id": deck_card_id,
            "quantity_in_deck": qty_in_deck,
            "generated_by": info["generated_by"],
        })

    # Orden estable: primero los que faltan, luego los que están, alfabético por nombre
    tokens_out.sort(key=lambda t: (t["in_deck"], t["name"].lower()))

    return {
        "tokens": tokens_out,
        "total_unique": len(tokens_out),
        "already_in_deck": already_count,
        "missing": len(tokens_out) - already_count,
    }


class TokensAddManyRequest(BaseModel):
    scryfall_ids: list[str]


@router.post("/{deck_id}/tokens-add-many", response_model=list[DeckCardView])
async def tokens_add_many(
    deck_id: int,
    payload: TokensAddManyRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[DeckCardView]:
    """Añade varios tokens al mazo de una vez con role='tokens'.

    - Idempotente: si un token ya está, lo salta (no incrementa quantity).
    - Los tokens NO cuentan para el mazo de 100 (role='tokens' está excluido
      de _COUNTING_ROLES_BY_FORMAT) pero SÍ van al PDF/XML (include=True).
    """
    if not payload.scryfall_ids:
        return []

    # ¿Qué scryfall_ids ya están?
    existing_ids = set((
            await db.scalars(
                select(DeckCard.scryfall_id).where(DeckCard.deck_id == deck_id)
            )
        ).all())

    # --- OPTIMIZACIÓN: batch prefetch de printings ya cacheados ---
    # Antes: db.get(PrintingCache, sfid) por cada id (N queries).
    # Ahora: 1 query WHERE IN, luego solo los que falten los pedimos a Scryfall.
    ids_to_check = [s for s in payload.scryfall_ids if s not in existing_ids]
    cached_printings: dict[str, PrintingCache] = {}
    if ids_to_check:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(ids_to_check))
            )
        ).all()
        cached_printings = {p.scryfall_id: p for p in rows}

    added: list[DeckCard] = []
    for sfid in payload.scryfall_ids:
        if sfid in existing_ids:
            continue
        cached = cached_printings.get(sfid)
        if not cached:
            raw = await scryfall.by_id(sfid)
            if not raw:
                continue
            cached = await deck_service.upsert_printing(db, raw)
        new_dc = DeckCard(
            deck_id=deck_id,
            oracle_id=cached.oracle_id or "",
            name=cached.name or "Token",
            quantity=1,
            scryfall_id=sfid,
            role="tokens",  # excluido del count del mazo, incluido en PDF/XML
            include=True,
        )
        db.add(new_dc)
        added.append(new_dc)
        existing_ids.add(sfid)

    if added:
        await deck_activity.log_event(
            db, deck_id, K.RELATED_ADDED,
            payload={
                "count": len(added),
                "kind": "tokens",
                "card_names": [dc.name for dc in added][:20],
            },
        )
    await db.commit()
    for dc in added:
        await db.refresh(dc)
    return await _deckcards_to_views(db, added)


@router.post("/{deck_id}/cards/{card_id}/add-related", response_model=list[DeckCardView])
async def add_related_cards(
    deck_id: int,
    card_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> list[DeckCardView]:
    """Añade automáticamente al mazo las cartas relacionadas (tokens, meld_result, meld_part).

    Se añaden con role="tokens" para que aparezcan en la sección Tokens y no
    cuenten para el mazo de 100. Cada una con quantity=1.
    Se omiten las que ya estén en el mazo.
    """
    import json as _json
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    printing = await db.get(PrintingCache, dc.scryfall_id)
    if not printing or not printing.related_parts:
        return []

    try:
        related = _json.loads(printing.related_parts)
    except (ValueError, TypeError):
        return []

    # ¿Qué scryfall_ids ya tiene el mazo?
    existing_ids = set((
            await db.scalars(select(DeckCard.scryfall_id).where(DeckCard.deck_id == deck_id))
        ).all())

    # --- OPTIMIZACIÓN: batch prefetch de printings ---
    # Antes: db.get(PrintingCache, sfid) por cada part (N queries en el bucle).
    # Ahora: 1 query WHERE IN por adelantado.
    candidate_sfids = {
        part.get("id") for part in related
        if part.get("id") and part["id"] not in existing_ids
    }
    cached_map: dict[str, PrintingCache] = {}
    if candidate_sfids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(candidate_sfids))
            )
        ).all()
        cached_map = {p.scryfall_id: p for p in rows}

    added: list[DeckCard] = []
    for part in related:
        sfid = part.get("id")
        if not sfid or sfid in existing_ids:
            continue
        # Asegurar que el printing está cacheado
        cached = cached_map.get(sfid)
        if not cached:
            raw = await scryfall.by_id(sfid)
            if not raw:
                continue
            cached = await deck_service.upsert_printing(db, raw)

        new_dc = DeckCard(
            deck_id=deck_id,
            oracle_id=cached.oracle_id or "",
            name=cached.name or part.get("name", ""),
            quantity=1,
            scryfall_id=sfid,
            role="tokens",  # se muestra en la sección Tokens y no cuenta para el 100
            include=True,
        )
        db.add(new_dc)
        added.append(new_dc)
        existing_ids.add(sfid)

    if added:
        # Contamos por componente para el summary (tokens vs meld_result…)
        components: dict[str, int] = {}
        for part in related:
            if part.get("id") in {a.scryfall_id for a in added}:
                components[part.get("component") or "related"] = \
                    components.get(part.get("component") or "related", 0) + 1
        await deck_activity.log_event(
            db, deck_id, K.RELATED_ADDED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload={
                "count": len(added),
                "kind": "related",
                "components": components,
                "trigger_card": dc.name,
                "card_names": [a.name for a in added][:20],
            },
        )
    await db.commit()
    for dc in added:
        await db.refresh(dc)
    return await _deckcards_to_views(db, added)


# ================================================================
