"""Lógica de importación y edición de mazos."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.moxfield import MoxfieldClient, normalize_deck, parse_plain_decklist
from mpc_forge.clients.scryfall import (
    ScryfallClient,
    is_double_faced,
    related_parts_from_card,
)
from mpc_forge.models import ArtPreference, Deck, DeckCard, PrintingCache

log = logging.getLogger(__name__)


def _normalize_card_name(name: str) -> str:
    """Lowercase, colapsa espacios, y unifica apóstrofes tipográficos (' → ')."""
    return (name or "").strip().lower().replace("’", "'").replace("`", "'")


async def upsert_printing(db: AsyncSession, card: dict[str, Any]) -> PrintingCache:
    """Crea/actualiza la entrada de PrintingCache a partir de un JSON de Scryfall."""
    scryfall_id = card["id"]
    obj = await db.get(PrintingCache, scryfall_id)
    front_img = card.get("image_uris") or {}
    back_img: dict[str, str] = {}
    back_name = None

    # Datos base de la carta (para tipo, coste, colores)
    mana_cost = card.get("mana_cost") or ""
    type_line = card.get("type_line") or ""
    colors = card.get("colors") or []

    if is_double_faced(card):
        faces = card.get("card_faces", [])
        front_img = faces[0].get("image_uris", front_img) if faces else front_img
        # En DFC, mana_cost/type/colors del frente están en la primera cara
        if faces:
            mana_cost = faces[0].get("mana_cost", mana_cost) or mana_cost
            type_line = faces[0].get("type_line", type_line) or type_line
            colors = faces[0].get("colors", colors) or colors
        if len(faces) > 1:
            back_img = faces[1].get("image_uris", {}) or {}
            back_name = faces[1].get("name")

    import json as _json
    related_parts = related_parts_from_card(card)
    related_parts_json = _json.dumps(related_parts, ensure_ascii=False) if related_parts else ""
    finishes_csv = ",".join(card.get("finishes", []) or [])
    keywords_csv = ",".join(card.get("keywords", []) or [])
    fields = {
        "oracle_id": card.get("oracle_id") or "",
        "name": card.get("name", ""),
        "set_code": (card.get("set") or "").lower(),
        "set_name": card.get("set_name") or "",
        "collector_number": card.get("collector_number") or "",
        "rarity": card.get("rarity") or "",
        "lang": card.get("lang") or "en",
        "frame": card.get("frame") or "",
        "border_color": card.get("border_color") or "",
        "full_art": bool(card.get("full_art", False)),
        "textless": bool(card.get("textless", False)),
        "promo": bool(card.get("promo", False)),
        "layout": card.get("layout") or "normal",
        "mana_cost": mana_cost,
        "cmc": float(card.get("cmc", 0.0) or 0.0),
        "type_line": type_line,
        "colors": ",".join(colors),
        "color_identity": ",".join(card.get("color_identity", []) or []),
        "keywords": keywords_csv,
        "image_normal": front_img.get("normal"),
        "image_large": front_img.get("large"),
        "image_png": front_img.get("png"),
        "back_image_normal": back_img.get("normal") if back_img else None,
        "back_image_large": back_img.get("large") if back_img else None,
        "back_image_png": back_img.get("png") if back_img else None,
        "back_name": back_name,
        "artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "finishes": finishes_csv,
        "related_parts": related_parts_json,
    }
    if obj is None:
        obj = PrintingCache(scryfall_id=scryfall_id, **fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)
    await db.flush()
    return obj


async def resolve_cards(
    db: AsyncSession, scryfall: ScryfallClient, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rellena scryfall_id / oracle_id / cache para cada entrada del decklist.

    Devuelve una lista con la misma cardinalidad + un campo `resolved: bool`.
    Adicionalmente pre-cachea las meld_result que se detecten (para que se
    puedan auto-añadir al mazo sin lookup extra).
    """
    to_lookup_by_id: list[dict[str, str]] = []
    to_lookup_by_name: list[dict[str, str]] = []
    for e in entries:
        if e.get("scryfall_id"):
            to_lookup_by_id.append({"id": e["scryfall_id"]})
        elif e.get("set") and e.get("number"):
            to_lookup_by_id.append({"set": e["set"], "collector_number": e["number"]})
        else:
            to_lookup_by_name.append({"name": e["name"]})

    resolved_map: dict[str, dict[str, Any]] = {}
    meld_result_ids: set[str] = set()

    if to_lookup_by_id or to_lookup_by_name:
        cards = await scryfall.collection(to_lookup_by_id + to_lookup_by_name)
        for c in cards:
            await upsert_printing(db, c)
            resolved_map[c["id"]] = c
            resolved_map[f"{c.get('set','')}:{c.get('collector_number','')}"] = c
            resolved_map[_normalize_card_name(c.get("name", ""))] = c
            # Detectar meld: recolectar los ids del meld_result para pre-cachear
            if c.get("layout") == "meld":
                for part in related_parts_from_card(c):
                    if part.get("component") == "meld_result":
                        meld_result_ids.add(part["id"])

    # Pre-cachear las meld_result (bulk lookup) para que create_deck_from_entries
    # pueda añadirlas sin más queries.
    if meld_result_ids:
        # Evitar re-descargar los que ya tenemos:
        need = []
        for sfid in meld_result_ids:
            if not await db.get(PrintingCache, sfid):
                need.append({"id": sfid})
        if need:
            extra = await scryfall.collection(need)
            for c in extra:
                await upsert_printing(db, c)

    out: list[dict[str, Any]] = []
    for e in entries:
        card: dict[str, Any] | None = None
        if e.get("scryfall_id"):
            card = resolved_map.get(e["scryfall_id"])
        if not card and e.get("set") and e.get("number"):
            card = resolved_map.get(f"{e['set']}:{e['number']}")
        if not card:
            card = resolved_map.get(_normalize_card_name(e["name"]))
        if card:
            out.append({
                **e,
                "scryfall_id": card["id"],
                "oracle_id": card.get("oracle_id", ""),
                "name": card.get("name", e["name"]),
                "resolved": True,
                "layout": card.get("layout", "normal"),
            })
        else:
            out.append({**e, "resolved": False})
    await db.commit()
    return out


async def create_deck_from_entries(
    db: AsyncSession,
    name: str,
    entries: list[dict[str, Any]],
    moxfield_id: str | None = None,
    source_url: str | None = None,
    fmt: str = "commander",
    include_extras: bool = False,
) -> Deck:
    """Crea un Deck aplicando preferencias de arte guardadas.

    include_extras=False (default): descarta companion/sideboard/tokens/maybeboard
    del mazo importado. Solo se conservan commander + mainboard. Esto reduce el
    ruido para el usuario que solo quiere imprimir el mazo principal.

    Extra: detecta cartas meld en el mazo y añade automáticamente sus
    resultados (p.ej. Bruna + Gisela → añade Brisela como token) para que el
    usuario no tenga que buscarlas a mano. Esto ocurre INCLUSO cuando
    include_extras=False, porque Brisela es parte necesaria del mazo.
    """
    import json as _json

    # Roles que siempre se importan: comandante y mazo principal.
    # El resto (companion/sideboard/tokens/maybeboard) solo si include_extras=True.
    _CORE_ROLES = {"commander", "mainboard"}

    deck = Deck(
        name=name,
        moxfield_id=moxfield_id,
        source_url=source_url,
        format=fmt,
    )
    db.add(deck)
    await db.flush()

    added_scryfall_ids: set[str] = set()

    for e in entries:
        if not e.get("resolved"):
            continue
        role = e.get("role", "mainboard")
        if not include_extras and role not in _CORE_ROLES:
            continue

        oracle_id = e.get("oracle_id", "")
        chosen = e["scryfall_id"]
        if oracle_id:
            pref = await db.get(ArtPreference, oracle_id)
            if pref:
                chosen = pref.scryfall_id
        dc = DeckCard(
            deck_id=deck.id,
            oracle_id=oracle_id,
            name=e["name"],
            quantity=e["quantity"],
            scryfall_id=chosen,
            role=role,
            include=True,
        )
        db.add(dc)
        added_scryfall_ids.add(chosen)
        if role == "commander":
            deck.commander_scryfall_id = chosen

    # Auto-añadir meld_result: siempre, incluso sin include_extras
    # (Brisela es parte del mazo tanto como Bruna).
    meld_results_added: set[str] = set()
    for e in entries:
        if not e.get("resolved"):
            continue
        role = e.get("role", "mainboard")
        if not include_extras and role not in _CORE_ROLES:
            continue
        printing = await db.get(PrintingCache, e["scryfall_id"])
        if not printing or printing.layout != "meld" or not printing.related_parts:
            continue
        try:
            related = _json.loads(printing.related_parts)
        except (ValueError, TypeError):
            continue
        for part in related:
            if part.get("component") != "meld_result":
                continue
            sfid = part.get("id")
            if not sfid or sfid in added_scryfall_ids or sfid in meld_results_added:
                continue
            cached = await db.get(PrintingCache, sfid)
            if not cached:
                continue
            db.add(DeckCard(
                deck_id=deck.id,
                oracle_id=cached.oracle_id or "",
                name=cached.name or part.get("name", ""),
                quantity=1,
                scryfall_id=sfid,
                role="tokens",
                include=True,
            ))
            meld_results_added.add(sfid)

    await db.commit()
    if meld_results_added:
        log.info("Auto-añadidos %d meld_result al mazo %s", len(meld_results_added), name)
    return deck


async def import_from_moxfield(
    db: AsyncSession,
    scryfall: ScryfallClient,
    mox: MoxfieldClient,
    url_or_id: str,
    include_extras: bool = False,
) -> Deck:
    payload = await mox.fetch_deck(url_or_id)
    norm = normalize_deck(payload)
    resolved = await resolve_cards(db, scryfall, norm["cards"])
    return await create_deck_from_entries(
        db,
        name=norm["name"],
        entries=resolved,
        moxfield_id=norm["moxfield_id"],
        source_url=norm["source_url"],
        fmt=norm["format"],
        include_extras=include_extras,
    )


async def import_from_plaintext(
    db: AsyncSession,
    scryfall: ScryfallClient,
    name: str,
    text: str,
    fmt: str = "commander",
    include_extras: bool = False,
) -> Deck:
    entries = parse_plain_decklist(text)
    for e in entries:
        e.setdefault("role", "mainboard")
    resolved = await resolve_cards(db, scryfall, entries)
    return await create_deck_from_entries(
        db, name=name, entries=resolved, fmt=fmt, include_extras=include_extras,
    )


async def fetch_printings_for_oracle(
    db: AsyncSession, scryfall: ScryfallClient, oracle_id: str
) -> list[PrintingCache]:
    """Devuelve todas las impresiones. Si aún no las tenemos, las bajamos."""
    rows = (
        await db.scalars(
            select(PrintingCache)
            .where(PrintingCache.oracle_id == oracle_id)
            .order_by(PrintingCache.released_at)
        )
    ).all()
    if len(rows) >= 2:
        # Heurística: si tenemos ≥2 impresiones asumimos que ya las bajamos
        return list(rows)
    prints = await scryfall.prints_by_oracle_id(oracle_id)
    for c in prints:
        await upsert_printing(db, c)
    await db.commit()
    rows = (
        await db.scalars(
            select(PrintingCache)
            .where(PrintingCache.oracle_id == oracle_id)
            .order_by(PrintingCache.released_at)
        )
    ).all()
    return list(rows)
