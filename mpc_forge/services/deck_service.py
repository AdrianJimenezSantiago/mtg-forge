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

    # --- OPTIMIZACIÓN: batch prefetch de ArtPreferences ---
    # En lugar de N queries db.get(ArtPreference, oid), traemos todas de golpe
    # con un WHERE ... IN (?). Un mazo commander tiene ~100 cartas → pasamos
    # de 100 queries a 1.
    oracle_ids_needed = {
        e.get("oracle_id", "") for e in entries
        if e.get("resolved") and e.get("oracle_id")
        and (include_extras or e.get("role", "mainboard") in _CORE_ROLES)
    }
    prefs_by_oracle: dict[str, str] = {}
    if oracle_ids_needed:
        rows = (
            await db.execute(
                select(ArtPreference.oracle_id, ArtPreference.scryfall_id)
                .where(ArtPreference.oracle_id.in_(oracle_ids_needed))
            )
        ).all()
        prefs_by_oracle = {oid: sfid for oid, sfid in rows}

    for e in entries:
        if not e.get("resolved"):
            continue
        role = e.get("role", "mainboard")
        if not include_extras and role not in _CORE_ROLES:
            continue

        oracle_id = e.get("oracle_id", "")
        chosen = prefs_by_oracle.get(oracle_id, e["scryfall_id"])
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

    # --- OPTIMIZACIÓN: prefetch batch de printings para detección de meld ---
    # Necesitamos leer .layout y .related_parts de cada carta que se añadió.
    # Antes: N queries db.get(PrintingCache, sfid). Ahora: 1 query WHERE IN.
    meld_results_added: set[str] = set()
    printings_map: dict[str, PrintingCache] = {}
    if added_scryfall_ids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(added_scryfall_ids))
            )
        ).all()
        printings_map = {p.scryfall_id: p for p in rows}

    # También pre-fetcheamos las meld_result que vayamos a necesitar. Los ids
    # los sabemos leyendo related_parts de cada printing meld.
    meld_result_ids_needed: set[str] = set()
    for e in entries:
        if not e.get("resolved"):
            continue
        role = e.get("role", "mainboard")
        if not include_extras and role not in _CORE_ROLES:
            continue
        printing = printings_map.get(e["scryfall_id"])
        if not printing or printing.layout != "meld" or not printing.related_parts:
            continue
        try:
            related = _json.loads(printing.related_parts)
        except (ValueError, TypeError):
            continue
        for part in related:
            if part.get("component") == "meld_result" and part.get("id"):
                meld_result_ids_needed.add(part["id"])

    meld_printings_map: dict[str, PrintingCache] = {}
    if meld_result_ids_needed:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(meld_result_ids_needed))
            )
        ).all()
        meld_printings_map = {p.scryfall_id: p for p in rows}

    # Auto-añadir meld_result: siempre, incluso sin include_extras
    # (Brisela es parte del mazo tanto como Bruna).
    for e in entries:
        if not e.get("resolved"):
            continue
        role = e.get("role", "mainboard")
        if not include_extras and role not in _CORE_ROLES:
            continue
        printing = printings_map.get(e["scryfall_id"])
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
            cached = meld_printings_map.get(sfid)
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


def _unresolved_from_entries(resolved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extrae las entradas que ``resolve_cards`` no consiguió resolver.

    Devuelve dicts con exactamente los campos que espera ``UnresolvedEntry``,
    para que las routes solo tengan que envolverlos en el schema Pydantic.
    """
    out: list[dict[str, Any]] = []
    for e in resolved:
        if e.get("resolved"):
            continue
        out.append({
            "name": e.get("name", "") or "",
            "quantity": int(e.get("quantity", 1) or 1),
            "raw_line": e.get("raw_line"),
            "set": e.get("set"),
            "number": e.get("number"),
            "role": e.get("role", "mainboard"),
            "reason": "not_found_on_scryfall",
        })
    return out


async def import_from_moxfield(
    db: AsyncSession,
    scryfall: ScryfallClient,
    mox: MoxfieldClient,
    url_or_id: str,
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    """Importa desde Moxfield y devuelve ``(deck, unresolved)``.

    ``unresolved`` es una lista de entradas que Scryfall no reconoció (rara
    para imports de Moxfield porque Moxfield ya trae scryfall_id, pero puede
    ocurrir con cartas muy nuevas aún no en Scryfall).
    """
    payload = await mox.fetch_deck(url_or_id)
    norm = normalize_deck(payload)
    resolved = await resolve_cards(db, scryfall, norm["cards"])
    unresolved = _unresolved_from_entries(resolved)
    deck = await create_deck_from_entries(
        db,
        name=norm["name"],
        entries=resolved,
        moxfield_id=norm["moxfield_id"],
        source_url=norm["source_url"],
        fmt=norm["format"],
        include_extras=include_extras,
    )
    return deck, unresolved


async def import_from_plaintext(
    db: AsyncSession,
    scryfall: ScryfallClient,
    name: str,
    text: str,
    fmt: str = "commander",
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    """Importa desde texto plano y devuelve ``(deck, unresolved)``.

    Las entradas no resueltas conservan la línea original (``raw_line``) para
    que el usuario vea exactamente qué texto no se pudo interpretar.
    """
    entries = parse_plain_decklist(text)
    for e in entries:
        e.setdefault("role", "mainboard")
    resolved = await resolve_cards(db, scryfall, entries)
    unresolved = _unresolved_from_entries(resolved)
    deck = await create_deck_from_entries(
        db, name=name, entries=resolved, fmt=fmt, include_extras=include_extras,
    )
    return deck, unresolved


async def try_localize_card(
    db: AsyncSession,
    scryfall: ScryfallClient,
    scryfall_id: str,
    lang: str,
) -> PrintingCache | None:
    """Intenta encontrar la versión localizada del printing dado.

    Estrategia: leer el printing en cache para obtener (set, collector_number)
    y pedir a Scryfall ``/cards/{set}/{number}/{lang}``. Si Scryfall responde,
    lo cacheamos como printing propio (con su scryfall_id de idioma) y lo
    devolvemos.

    Devuelve ``None`` si:
    - el printing base no está en cache (no debería pasar tras un import normal)
    - la impresión no existe en ese idioma en Scryfall (secret lairs, promos…)
    - hay un error de red
    """
    if lang == "en":
        # Los printings ingleses son "el default" de Scryfall — no requiere lookup extra
        return await db.get(PrintingCache, scryfall_id)

    base = await db.get(PrintingCache, scryfall_id)
    if not base or not base.set_code or not base.collector_number:
        return None
    # Si ya está en el idioma pedido, no hace falta llamar a Scryfall
    if base.lang == lang:
        return base

    # ¿Ya lo tenemos cacheado bajo el mismo (set, collector_number, lang)?
    # PrintingCache no está indexado por (set, number, lang) — hacemos scan.
    # En la práctica hay pocas rows por oracle_id, así que compensa filtrar por
    # oracle_id primero (que sí está indexado).
    if base.oracle_id:
        candidates = (
            await db.scalars(
                select(PrintingCache).where(
                    PrintingCache.oracle_id == base.oracle_id,
                    PrintingCache.set_code == base.set_code,
                    PrintingCache.collector_number == base.collector_number,
                    PrintingCache.lang == lang,
                )
            )
        ).all()
        if candidates:
            return candidates[0]

    try:
        raw = await scryfall.by_set_and_number(base.set_code, base.collector_number, lang=lang)
    except Exception as e:  # noqa: BLE001
        log.debug("Localización de %s/%s a %s falló: %s",
                  base.set_code, base.collector_number, lang, e)
        return None
    if not raw:
        return None

    return await upsert_printing(db, raw)


async def localize_deck(
    db: AsyncSession,
    scryfall: ScryfallClient,
    deck_id: int,
    lang: str,
) -> dict[str, Any]:
    """Aplica el idioma pedido a TODAS las cartas del mazo.

    Para cada carta:
    - Si ya está en el idioma pedido → no toca nada
    - Si no y Scryfall tiene esa versión → actualiza ``DeckCard.scryfall_id`` al
      printing localizado (que ya se cachea con su nuevo scryfall_id)
    - Si Scryfall no tiene esa versión → conserva la impresión actual y la
      añade al reporte de "no disponibles" que se devuelve

    NOTA: se saltan las cartas con custom_art_front_id — su arte de frente lo
    define el custom, no la impresión oficial. Cambiarles el scryfall_id
    dejaría la carta con custom art delante pero metadata (nombre localizado,
    reverso DFC) del nuevo idioma, lo cual es confuso.

    Devuelve un resumen: ``{localized, unchanged, unavailable: [names]}``.
    """
    cards = (
        await db.scalars(
            select(DeckCard).where(DeckCard.deck_id == deck_id)
        )
    ).all()

    # --- OPTIMIZACIÓN: batch prefetch de printings actuales ---
    # Antes: db.get(PrintingCache, dc.scryfall_id) por cada carta (N queries).
    # Ahora: 1 query WHERE IN para todas las de golpe.
    current_sfids = {dc.scryfall_id for dc in cards if not dc.custom_art_front_id}
    current_by_sfid: dict[str, PrintingCache] = {}
    if current_sfids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(current_sfids))
            )
        ).all()
        current_by_sfid = {p.scryfall_id: p for p in rows}

    localized = 0
    unchanged = 0
    unavailable: list[str] = []
    skipped_custom = 0

    for dc in cards:
        if dc.custom_art_front_id:
            # Respetamos el arte custom del usuario — no cambiamos scryfall_id
            skipped_custom += 1
            continue

        current = current_by_sfid.get(dc.scryfall_id)
        if current and current.lang == lang:
            unchanged += 1
            continue

        localized_printing = await try_localize_card(db, scryfall, dc.scryfall_id, lang)
        if localized_printing is None:
            unavailable.append(dc.name)
            continue
        if localized_printing.scryfall_id == dc.scryfall_id:
            unchanged += 1
            continue
        dc.scryfall_id = localized_printing.scryfall_id
        localized += 1

    await db.commit()
    return {
        "lang": lang,
        "localized": localized,
        "unchanged": unchanged,
        "unavailable": unavailable,
        "skipped_custom": skipped_custom,
    }


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
