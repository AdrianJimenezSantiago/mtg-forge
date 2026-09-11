"""Selector de artes, precarga en segundo plano y recomendadores.

Extraído de `routes/decks.py` durante la división en sub-routers. La lógica no
ha cambiado.
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import (
    Depends, HTTPException, Query, status,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.db import get_session
from mpc_forge.models import (
    ArtPreference, CustomArt, Deck, DeckCard, LocalArt, PrintingCache,
)
from mpc_forge.schemas import (
    ArtOption,
    ArtOptionsPage,
    ChangeArtRequest,
    DeckCardView,
)
from mpc_forge.services import (
    custom_art, deck_activity, deck_service, history,
    preloader, thumbnails,
)
from mpc_forge.services.deck_activity import DeckActivityKind as K


log = logging.getLogger(__name__)


# --- Import / CRUD -------------------------------------------------------

from mpc_forge.routes.decks._views import (
    _deckcard_to_view,
)
from mpc_forge.routes.decks._common import (
    DbDep, _get_scryfall, make_router,
)

router = make_router()


# Orden de rareza para el sort "rarity". Se define aquí y no en la BD porque
# es una preferencia de presentación, no un dato del dominio.
_RARITY_RANK = {
    "mythic": 0, "rare": 1, "special": 2, "bonus": 3,
    "uncommon": 4, "common": 5,
}

# Claves de ordenación admitidas. Se valida contra este dict en vez de
# interpolar el parámetro: así un `sort` arbitrario no llega nunca a la lógica.
_SORT_KEYS = {
    # Más reciente primero — el default: casi siempre quieres la impresión
    # nueva, y las alternativas modernas suelen ser las más vistosas.
    "released_desc": lambda o: (o.released_at or "", o.set_code),
    "released_asc":  lambda o: (o.released_at or "9999", o.set_code),
    "set":           lambda o: (o.set_code, o.collector_number),
    "rarity":        lambda o: (_RARITY_RANK.get(o.rarity, 9), o.released_at or ""),
    "artist":        lambda o: ((o.artist or "zzz").lower(), o.released_at or ""),
}
_REVERSED_SORTS = {"released_desc"}

# Facetas admitidas en el parámetro `only`, con su predicado.
_FACET_PREDICATES = {
    "full_art":   lambda o: o.full_art,
    "textless":   lambda o: o.textless,
    "promo":      lambda o: o.promo,
    "borderless": lambda o: o.border_color == "borderless",
    # Los marcos "1993" y "1997" son los retro clásicos según Scryfall.
    "retro":      lambda o: o.frame in ("1993", "1997"),
}


def _thumb_for_local(art_row) -> str | None:
    """URL de miniatura para un LocalArt ya descargado, o None."""
    if art_row is None or not art_row.relative_path:
        return None
    return thumbnails.url_for_relative(art_row.relative_path)


@router.get("/{deck_id}/cards/{card_id}/prints", response_model=ArtOptionsPage)
async def list_printings_for_card(
    deck_id: int,
    card_id: int,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=300),
    sort: str = Query("released_desc"),
    q: str = Query("", max_length=120),
    only: str = Query("", max_length=200),
) -> ArtOptionsPage:
    """Opciones de arte para una carta, paginadas y filtrables.

    Antes esto devolvía TODAS las opciones en una respuesta: para una carta
    como Sol Ring son ~900 impresiones y unos 400 KB de JSON que el frontend
    recibía enteros para luego trocearlos en cliente. Ahora la paginación es
    real y el filtrado ocurre en el servidor.

    Los artes custom del usuario se devuelven completos en ``custom``, al
    margen de la paginación: son pocos, son los que más le interesan, y
    mezclarlos en la misma secuencia hacía que "página 3" significara cosas
    distintas según cuántos customs hubiera.

    ``only`` es una lista separada por comas de facetas exigidas
    (``full_art,textless,promo,borderless,retro``); se combinan con AND.
    ``q`` filtra por nombre de set, código, número de coleccionista o artista.
    """
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    if sort not in _SORT_KEYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"sort invalido: {sort!r}. Validos: {', '.join(sorted(_SORT_KEYS))}",
        )

    # --- 1) Artes custom (frente + reverso), siempre completos -------------
    custom_options: list[ArtOption] = []
    for face in ("front", "back"):
        for ca in await custom_art.find_for_card(db, dc.name, face=face):
            is_chosen = (
                (face == "front" and dc.custom_art_front_id == ca.id)
                or (face == "back" and dc.custom_art_back_id == ca.id)
            )
            custom_options.append(ArtOption(
                kind="custom",
                custom_art_id=ca.id,
                variant_label=ca.variant_label,
                filename=ca.filename,
                face=face,
                image_small=custom_art.custom_art_url(ca.relative_path),
                thumb_url=thumbnails.url_for_relative(ca.relative_path),
                is_chosen=is_chosen,
            ))

    # --- 2) Impresiones oficiales ------------------------------------------
    prints = await deck_service.fetch_printings_for_oracle(db, scryfall, dc.oracle_id)
    pref = await db.get(ArtPreference, dc.oracle_id) if dc.oracle_id else None
    last_used = (
        await history.last_scryfall_id_used(db, dc.oracle_id) if dc.oracle_id else None
    )

    # Miniaturas: UNA query para todos los LocalArt de estas impresiones, en
    # vez de un lookup por opción. En un endpoint que devuelve cientos de filas
    # el N+1 aquí sería devastador.
    print_ids = [p.scryfall_id for p in prints]
    local_arts: dict[str, object] = {}
    if print_ids:
        rows = (await db.execute(
            select(LocalArt).where(
                LocalArt.scryfall_id.in_(print_ids), LocalArt.face == "front"
            )
        )).scalars().all()
        local_arts = {row.scryfall_id: row for row in rows}

    options: list[ArtOption] = []
    for p in prints:
        options.append(ArtOption(
            kind="scryfall",
            scryfall_id=p.scryfall_id,
            set_code=p.set_code,
            set_name=p.set_name,
            collector_number=p.collector_number,
            frame=p.frame,
            border_color=p.border_color,
            full_art=p.full_art,
            textless=p.textless,
            promo=p.promo,
            layout=p.layout,
            artist=p.artist,
            released_at=p.released_at,
            rarity=p.rarity,
            face="front",
            image_small=p.image_normal,
            thumb_url=_thumb_for_local(local_arts.get(p.scryfall_id)),
            is_chosen=(
                dc.custom_art_front_id is None and p.scryfall_id == dc.scryfall_id
            ),
            is_preferred=(pref is not None and pref.scryfall_id == p.scryfall_id),
            is_last_used=(last_used is not None and last_used == p.scryfall_id),
        ))

    # --- 3) Facetas ANTES de filtrar ---------------------------------------
    # Los contadores deben reflejar el conjunto completo: si el usuario ya ha
    # filtrado por "full art", el contador de "textless" tiene que seguir
    # diciéndole cuántos hay en total, no cuántos quedan tras su filtro.
    facets = {
        "total": len(options),
        "custom": len(custom_options),
    }
    for name, predicate in _FACET_PREDICATES.items():
        facets[name] = sum(1 for o in options if predicate(o))

    # --- 4) Filtros ---------------------------------------------------------
    wanted = {t.strip() for t in only.split(",") if t.strip()}
    if wanted:
        unknown = wanted - _FACET_PREDICATES.keys()
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Facetas desconocidas: {', '.join(sorted(unknown))}",
            )
        # AND entre facetas: "full art Y textless" es lo que la gente espera.
        # Con OR, marcar dos casillas devolvía casi todo el conjunto.
        options = [
            o for o in options
            if all(_FACET_PREDICATES[w](o) for w in wanted)
        ]

    if q:
        needle = q.strip().lower()
        options = [
            o for o in options
            if needle in o.set_name.lower()
            or needle in o.set_code.lower()
            or needle in o.collector_number.lower()
            or needle in (o.artist or "").lower()
        ]

    # --- 5) Orden y paginación ---------------------------------------------
    options.sort(key=_SORT_KEYS[sort], reverse=sort in _REVERSED_SORTS)

    total = len(options)
    page = options[offset:offset + limit]

    return ArtOptionsPage(
        items=page,
        # Los customs solo viajan en la primera página: repetirlos en cada
        # scroll infinito los duplicaría en la rejilla.
        custom=custom_options if offset == 0 else [],
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + limit) < total,
        facets=facets,
    )


# ================================================================
# PRECARGA DE PRINTS EN BACKGROUND
# ================================================================
# Cuando el usuario abre un mazo, disparamos precarga de todas las
# impresiones alternativas (fetch_printings_for_oracle) en background.
# Así cuando abre el modal de arte para cualquier carta, ya está cacheado
# y la respuesta es instantánea desde BD (sin llamar a Scryfall).

@router.post("/{deck_id}/preload-prints")
async def preload_prints(
    deck_id: int,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> dict:
    """Arranca precarga en background. Devuelve el estado inicial (total, done=0).

    Si ya hay una precarga en curso para este mazo, la cancela y arranca una nueva.
    Los oracle_ids ya cacheados (>=2 prints en BD) se procesan en <10ms cada uno;
    solo los no cacheados llaman a Scryfall API.
    """
    state = await preloader.start(deck_id, scryfall)
    return state.to_dict()


@router.get("/{deck_id}/preload-progress")
async def preload_progress(deck_id: int) -> dict:
    """Estado de la precarga (para polling desde el frontend)."""
    state = preloader.get_state(deck_id)
    if state is None:
        return {"deck_id": deck_id, "total": 0, "done": 0, "in_progress": False}
    return state.to_dict()


@router.post("/{deck_id}/preload-cancel")
async def preload_cancel(deck_id: int) -> dict:
    """Cancela la precarga en curso (ej. cuando el user cambia de mazo)."""
    await preloader.cancel(deck_id)
    return {"deck_id": deck_id, "cancelled": True}


@router.post("/{deck_id}/cards/change-art", response_model=DeckCardView)
async def change_art(
    deck_id: int,
    payload: ChangeArtRequest,
    db: DbDep,
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> DeckCardView:
    """Cambia el arte seleccionado para una carta del mazo.

    - Si `custom_art_id` está poblado: usa ese arte custom en la cara indicada.
    - Si `scryfall_id` está poblado: usa ese arte oficial y limpia el custom
      correspondiente (para front). Optional: recordar globalmente.
    """
    dc = await db.get(DeckCard, payload.deck_card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")

    # Snapshot ANTES de mutar. Guardamos el set/número para el timeline —
    # es lo más útil para el usuario, más que el scryfall_id.
    old_sfid = dc.scryfall_id
    old_custom_front = dc.custom_art_front_id
    old_custom_back = dc.custom_art_back_id
    old_printing = await db.get(PrintingCache, old_sfid) if old_sfid else None
    old_set = old_printing.set_code if old_printing else None
    old_number = old_printing.collector_number if old_printing else None

    if payload.custom_art_id is not None:
        ca = await db.get(CustomArt, payload.custom_art_id)
        if not ca:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "custom_art_id inválido")
        if payload.face == "back":
            dc.custom_art_back_id = ca.id
        else:
            dc.custom_art_front_id = ca.id
    elif payload.scryfall_id is not None:
        # Elección oficial: limpia el custom del frente (o back) y actualiza scryfall_id
        printing = await db.get(PrintingCache, payload.scryfall_id)
        if not printing:
            raw = await scryfall.by_id(payload.scryfall_id)
            if not raw:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "scryfall_id inválido")
            await deck_service.upsert_printing(db, raw)
        if payload.face == "back":
            dc.custom_art_back_id = None
            # El scryfall_id define la carta completa (front+back del DFC), no lo cambiamos aquí
        else:
            dc.custom_art_front_id = None
            dc.scryfall_id = payload.scryfall_id
            if payload.remember_globally and dc.oracle_id:
                pref = await db.get(ArtPreference, dc.oracle_id)
                if pref:
                    pref.scryfall_id = payload.scryfall_id
                else:
                    db.add(ArtPreference(oracle_id=dc.oracle_id, scryfall_id=payload.scryfall_id))
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Debe indicarse scryfall_id o custom_art_id"
        )

    # Reunimos el nuevo estado para el payload del timeline.
    new_printing = await db.get(PrintingCache, dc.scryfall_id) if dc.scryfall_id else None
    activity_payload = {
        "face": payload.face,
        "kind": "custom" if payload.custom_art_id else "official",
    }
    if payload.custom_art_id:
        ca = await db.get(CustomArt, payload.custom_art_id)
        activity_payload.update({
            "custom_art_id": payload.custom_art_id,
            "custom_filename": ca.filename if ca else None,
            "custom_variant": ca.variant_label if ca else None,
        })
    else:
        activity_payload.update({
            "old_scryfall_id": old_sfid,
            "new_scryfall_id": dc.scryfall_id,
            "old_set": old_set, "old_number": old_number,
            "new_set": new_printing.set_code if new_printing else None,
            "new_number": new_printing.collector_number if new_printing else None,
            "remember_globally": payload.remember_globally,
        })
    # Solo loggeamos si realmente cambió algo (evita ruido si el usuario
    # hace click en el arte que ya estaba seleccionado).
    changed = (
        dc.scryfall_id != old_sfid
        or dc.custom_art_front_id != old_custom_front
        or dc.custom_art_back_id != old_custom_back
    )
    if changed:
        await deck_activity.log_event(
            db, deck_id, K.CARD_ART_CHANGED,
            card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
            payload=activity_payload,
        )
    await db.commit()
    return await _deckcard_to_view(db, dc)


@router.post("/{deck_id}/cards/{card_id}/toggle", response_model=DeckCardView)
async def toggle_include(deck_id: int, card_id: int, db: DbDep) -> DeckCardView:
    dc = await db.get(DeckCard, card_id)
    if not dc or dc.deck_id != deck_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Carta no encontrada")
    dc.include = not dc.include
    await deck_activity.log_event(
        db, deck_id, K.CARD_INCLUDE_TOGGLED,
        card_name=dc.name, card_scryfall_id=dc.scryfall_id, card_oracle_id=dc.oracle_id,
        payload={"new_include": dc.include, "role": dc.role, "quantity": dc.quantity},
    )
    await db.commit()
    return await _deckcard_to_view(db, dc)


# --- Helpers de vista -----------------------------------------------------

class ArtistRecommendRequest(BaseModel):
    """Payload del recomendador. Solo requiere ``artist``; los oracle_ids
    se derivan del deck en el servidor (evita al frontend enviarlos)."""
    artist: str
    # Opcional: filtro por rol para acotar (mainboard, commander, all).
    role: str = "all"


class ArtistMatchView(BaseModel):
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None = None
    image_normal: str | None = None
    released_at: str | None = None
    is_full_art: bool = False
    is_promo: bool = False


class ArtistRecommendResponse(BaseModel):
    artist_query: str
    matched: list[ArtistMatchView]
    unmatched_count: int
    skipped_count: int
    total_deck_uniques: int


@router.post(
    "/{deck_id}/recommend-by-artist",
    response_model=ArtistRecommendResponse,
)
async def recommend_by_artist(
    deck_id: int,
    payload: ArtistRecommendRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> ArtistRecommendResponse:
    """Sugiere impresiones del mazo hechas por ``artist``.

    Uso típico: el usuario elige un arte de X para su carta A y quiere ver
    qué OTRAS cartas del mazo tienen también arte de X. El endpoint devuelve
    la mejor cover por cada carta (regular > full art > promo; más reciente).

    Limitado a `MAX_ORACLES_PER_REQUEST` cartas únicas por request (~25);
    el resto vuelve en `skipped_count` para que la UI ofrezca "cargar más".
    """
    from mpc_forge.services.recommender import recommend_by_artist as _rec

    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    # Filtro de rol si el usuario acotó ("commander", "mainboard", "all").
    role = (payload.role or "all").lower()
    if role not in {"all", "mainboard", "commander", "sideboard"}:
        role = "all"

    # DeckCard ya persiste el oracle_id de cada carta al importar — no
    # necesitamos JOIN a PrintingCache, evitando N+1 y una query extra.
    stmt = select(DeckCard.oracle_id).where(
        DeckCard.deck_id == deck_id, DeckCard.include.is_(True)
    )
    if role != "all":
        stmt = stmt.where(DeckCard.role == role)
    rows = (await db.execute(stmt)).all()
    oracle_ids = [r[0] for r in rows if r[0]]
    total_unique = len(set(oracle_ids))

    result = await _rec(scryfall, oracle_ids, payload.artist, db=db)
    return ArtistRecommendResponse(
        artist_query=result.artist_query,
        matched=[ArtistMatchView(**vars(m)) for m in result.matched],
        unmatched_count=len(result.unmatched),
        skipped_count=len(result.skipped),
        total_deck_uniques=total_unique,
    )


# ---- Recomendador por estilo (Extras · F3/T11) ------------------------------


class StyleRecommendRequest(BaseModel):
    """Payload para recommend-by-style. Al menos un criterio debe activarse."""
    set_code: str | None = None
    borderless: bool = False
    showcase: bool = False
    extended: bool = False
    full_art: bool = False
    role: str = "all"


class StyleMatchView(BaseModel):
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None = None
    image_normal: str | None = None
    released_at: str | None = None
    matched_criteria: list[str]


class StyleRecommendResponse(BaseModel):
    query: dict
    matched: list[StyleMatchView]
    unmatched_count: int
    skipped_count: int
    total_deck_uniques: int


@router.post(
    "/{deck_id}/recommend-by-style",
    response_model=StyleRecommendResponse,
)
async def recommend_by_style_endpoint(
    deck_id: int,
    payload: StyleRecommendRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    scryfall: Annotated[ScryfallClient, Depends(_get_scryfall)],
) -> StyleRecommendResponse:
    """Extras · F3/T11: recomienda impresiones que cumplen criterios de estilo.

    Al menos uno de ``set_code / borderless / showcase / extended / full_art``
    debe estar activo. Los criterios se combinan con AND.
    """
    from mpc_forge.services.recommender import recommend_by_style

    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mazo no encontrado")

    role = (payload.role or "all").lower()
    if role not in {"all", "mainboard", "commander", "sideboard"}:
        role = "all"

    stmt = select(DeckCard.oracle_id).where(
        DeckCard.deck_id == deck_id, DeckCard.include.is_(True),
    )
    if role != "all":
        stmt = stmt.where(DeckCard.role == role)
    rows = (await db.execute(stmt)).all()
    oracle_ids = [r[0] for r in rows if r[0]]
    total_unique = len(set(oracle_ids))

    result = await recommend_by_style(
        scryfall, oracle_ids,
        set_code=payload.set_code,
        borderless=payload.borderless,
        showcase=payload.showcase,
        extended=payload.extended,
        full_art=payload.full_art,
        db=db,
    )
    return StyleRecommendResponse(
        query=result.query,
        matched=[StyleMatchView(**vars(m)) for m in result.matched],
        unmatched_count=len(result.unmatched),
        skipped_count=len(result.skipped),
        total_deck_uniques=total_unique,
    )
