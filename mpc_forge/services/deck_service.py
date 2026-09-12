"""Lógica de importación y edición de mazos."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.moxfield import MoxfieldClient, normalize_deck, parse_plain_decklist
from mpc_forge.clients.scryfall import (
    ScryfallClient,
    is_double_faced,
    related_parts_from_card,
)
from mpc_forge.models import ArtPreference, BulkSyncState, Deck, DeckCard, PrintingCache

log = logging.getLogger(__name__)

# SQLite limita el número de parámetros por sentencia; los ``IN (...)`` grandes
# se trocean para no rozar ese límite en instalaciones con SQLite antiguo.
_IN_CHUNK = 500

# Una impresión cacheada se da por buena para resolver imports durante este
# tiempo. Scryfall pide cachear al menos 24 h, y los datos que usa el import
# (id, oracle_id, nombre, layout, partes relacionadas) casi nunca cambian.
RESOLVE_CACHE_TTL = timedelta(days=7)

# Memoria de proceso: nombre normalizado → scryfall_id que devolvió Scryfall.
# Permite que el segundo import con Sol Ring no vuelva a preguntar por él, y
# garantiza que se elige la MISMA impresión que habría elegido Scryfall.
_NAME_MEMO_TTL = 24 * 3600.0
_NAME_MEMO_MAX = 20_000
_name_memo: dict[str, tuple[str, float]] = {}

# Oracle_ids cuyas impresiones completas ya se descargaron en este proceso.
_PRINTS_MEMO_TTL = 24 * 3600.0
_prints_complete: dict[str, float] = {}


def _normalize_card_name(name: str) -> str:
    """Lowercase, colapsa espacios, y unifica apóstrofes tipográficos (' → ')."""
    return (name or "").strip().lower().replace("’", "'").replace("`", "'")


def _name_keys(name: str) -> list[str]:
    """Claves de búsqueda por nombre: el nombre completo y cada cara.

    ``"Delver of Secrets // Insectile Aberration"`` → nombre completo,
    ``delver of secrets`` e ``insectile aberration``. Así una lista que solo
    escribe la cara frontal (formato Arena, MTGO…) casa con la respuesta de
    Scryfall, que siempre devuelve el nombre completo.
    """
    full = _normalize_card_name(name)
    keys = [full]
    if " // " in full:
        keys.extend(part.strip() for part in full.split(" // ") if part.strip())
    return keys


def _memo_name(name: str, scryfall_id: str) -> None:
    if len(_name_memo) >= _NAME_MEMO_MAX:
        _name_memo.clear()
    now = time.monotonic()
    for key in _name_keys(name):
        _name_memo.setdefault(key, (scryfall_id, now))


def _memo_lookup(name: str) -> str | None:
    hit = _name_memo.get(_normalize_card_name(name))
    if not hit:
        return None
    sfid, at = hit
    if time.monotonic() - at > _NAME_MEMO_TTL:
        _name_memo.pop(_normalize_card_name(name), None)
        return None
    return sfid


def _as_price(raw: Any) -> float | None:
    """Convierte un precio de Scryfall a float.

    Scryfall los manda como cadena ("12.34") o `null`. Una cadena vacía o un
    valor no numérico se tratan como "sin precio" en vez de reventar el
    import: un precio ausente nunca debe impedir añadir una carta al mazo.
    """
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _as_aware(dt: datetime | None) -> datetime | None:
    """Red de seguridad para datetimes que no vienen de la BD.

    Las columnas usan ``models.TZDateTime``, que ya devuelve valores aware en
    UTC, así que para filas leídas del ORM esto es un no-op. Se mantiene para
    los datetimes que llegan de fuera (parseo de JSON de Scryfall, valores
    construidos en tests) y que sí pueden venir naive.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _chunks(items: list[Any], size: int = _IN_CHUNK) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _bulk_synced(db: AsyncSession) -> bool:
    """True si el usuario importó el volcado completo de Scryfall (modo offline)."""
    return (await db.get(BulkSyncState, "default_cards")) is not None


def _printing_fields(card: dict[str, Any]) -> dict[str, Any]:
    """Columnas de ``PrintingCache`` a partir de un JSON de carta de Scryfall."""
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

    related_parts = related_parts_from_card(card)
    prices = card.get("prices") or {}
    return {
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
        "keywords": ",".join(card.get("keywords", []) or []),
        "image_normal": front_img.get("normal"),
        "image_large": front_img.get("large"),
        "image_png": front_img.get("png"),
        "back_image_normal": back_img.get("normal") if back_img else None,
        "back_image_large": back_img.get("large") if back_img else None,
        "back_image_png": back_img.get("png") if back_img else None,
        "back_name": back_name,
        "artist": card.get("artist"),
        "released_at": card.get("released_at"),
        "finishes": ",".join(card.get("finishes", []) or []),
        "price_usd": _as_price(prices.get("usd")),
        "price_usd_foil": _as_price(prices.get("usd_foil")),
        "price_eur": _as_price(prices.get("eur")),
        "legalities": (
            json.dumps(card.get("legalities") or {}, ensure_ascii=False)
            if card.get("legalities") else ""
        ),
        "related_parts": json.dumps(related_parts, ensure_ascii=False) if related_parts else "",
        "fetched_at": datetime.now(UTC),
    }


async def upsert_printing(db: AsyncSession, card: dict[str, Any]) -> PrintingCache:
    """Crea/actualiza la entrada de PrintingCache a partir de un JSON de Scryfall."""
    scryfall_id = card["id"]
    obj = await db.get(PrintingCache, scryfall_id)
    fields = _printing_fields(card)
    if obj is None:
        obj = PrintingCache(scryfall_id=scryfall_id, **fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)
    await db.flush()
    return obj


async def upsert_printings(
    db: AsyncSession, cards: Iterable[dict[str, Any]]
) -> dict[str, PrintingCache]:
    """Versión por lotes de :func:`upsert_printing`.

    Una sola consulta ``IN`` para cargar las filas existentes y un solo
    ``flush`` al final, en vez de un ``SELECT`` + ``flush`` por carta. Con las
    ~150 impresiones de un Sol Ring la diferencia es de un orden de magnitud.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for c in cards:
        if c.get("id"):
            by_id[c["id"]] = c
    if not by_id:
        return {}

    existing: dict[str, PrintingCache] = {}
    for chunk in _chunks(list(by_id)):
        for row in (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all():
            existing[row.scryfall_id] = row

    out: dict[str, PrintingCache] = {}
    for sfid, card in by_id.items():
        fields = _printing_fields(card)
        obj = existing.get(sfid)
        if obj is None:
            obj = PrintingCache(scryfall_id=sfid, **fields)
            db.add(obj)
        else:
            for k, v in fields.items():
                setattr(obj, k, v)
        out[sfid] = obj
    await db.flush()
    return out


def _row_as_card(row: PrintingCache) -> dict[str, Any]:
    """Vista mínima de una fila de ``PrintingCache`` con la forma de un JSON de
    Scryfall: solo los campos que usa :func:`resolve_cards`."""
    try:
        parts = json.loads(row.related_parts) if row.related_parts else []
    except (ValueError, TypeError):
        parts = []
    return {
        "id": row.scryfall_id,
        "oracle_id": row.oracle_id,
        "name": row.name,
        "set": row.set_code,
        "collector_number": row.collector_number,
        "layout": row.layout,
        "_related_parts": parts,
    }


def _meld_result_ids(card: dict[str, Any]) -> list[str]:
    if card.get("layout") != "meld":
        return []
    parts = card.get("_related_parts")
    if parts is None:
        parts = related_parts_from_card(card)
    return [p["id"] for p in parts if p.get("component") == "meld_result" and p.get("id")]


async def _resolve_from_cache(
    db: AsyncSession, entries: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], set[int]]:
    """Intenta resolver las entradas sin red, contra ``PrintingCache``.

    * Por ``scryfall_id`` o ``(set, número)``: si la fila existe y es reciente
      (o viene del volcado completo), se usa directamente.
    * Por nombre: solo si Scryfall ya resolvió ese nombre en este proceso
      (:data:`_name_memo`). No se elige una impresión «parecida» del caché,
      porque podría no ser la que Scryfall elegiría por defecto y cambiaría el
      arte inicial del mazo.

    Devuelve ``(cards_by_key, indices_resueltos)``.
    """
    bulk = await _bulk_synced(db)
    cutoff = datetime.now(UTC) - RESOLVE_CACHE_TTL

    def fresh(row: PrintingCache) -> bool:
        if not row.oracle_id:
            return False
        if bulk:
            return True
        fetched = _as_aware(row.fetched_at)
        return fetched is not None and fetched >= cutoff

    wanted_ids: set[str] = set()
    wanted_sets: set[str] = set()
    for e in entries:
        if e.get("scryfall_id"):
            wanted_ids.add(e["scryfall_id"])
        elif e.get("set") and e.get("number"):
            wanted_sets.add(e["set"])
        else:
            memo = _memo_lookup(e.get("name", ""))
            if memo:
                wanted_ids.add(memo)

    by_id: dict[str, PrintingCache] = {}
    for chunk in _chunks(list(wanted_ids)):
        for row in (
            await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        ).all():
            by_id[row.scryfall_id] = row

    by_set_num: dict[tuple[str, str], PrintingCache] = {}
    if wanted_sets:
        numbers = {e["number"] for e in entries if e.get("set") and e.get("number")}
        for chunk in _chunks(list(numbers)):
            rows = (
                await db.scalars(
                    select(PrintingCache).where(
                        PrintingCache.set_code.in_(wanted_sets),
                        PrintingCache.collector_number.in_(chunk),
                        PrintingCache.lang == "en",
                    )
                )
            ).all()
            for row in rows:
                by_set_num[(row.set_code, row.collector_number)] = row

    cards: dict[str, dict[str, Any]] = {}
    hits: set[int] = set()
    for idx, e in enumerate(entries):
        row: PrintingCache | None = None
        if e.get("scryfall_id"):
            row = by_id.get(e["scryfall_id"])
        elif e.get("set") and e.get("number"):
            row = by_set_num.get((e["set"], e["number"]))
        else:
            memo = _memo_lookup(e.get("name", ""))
            row = by_id.get(memo) if memo else None
        if row is not None and fresh(row):
            cards[f"idx:{idx}"] = _row_as_card(row)
            hits.add(idx)
    return cards, hits


async def resolve_cards(
    db: AsyncSession, scryfall: ScryfallClient, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rellena scryfall_id / oracle_id / cache para cada entrada del decklist.

    Devuelve una lista con la misma cardinalidad + un campo `resolved: bool`.
    Adicionalmente pre-cachea las meld_result que se detecten (para que se
    puedan auto-añadir al mazo sin lookup extra).

    Orden de resolución, de más barato a más caro:
      1. ``PrintingCache`` local (ver :func:`_resolve_from_cache`).
      2. ``POST /cards/collection`` solo para lo que falte, sin duplicados.

    Importar diez mazos Commander seguidos que comparten staples pasa así de
    ~20 peticiones a Scryfall a las estrictamente necesarias.
    """
    cached_cards, cache_hits = await _resolve_from_cache(db, entries)

    to_lookup: list[dict[str, str]] = []
    for idx, e in enumerate(entries):
        if idx in cache_hits:
            continue
        if e.get("scryfall_id"):
            to_lookup.append({"id": e["scryfall_id"]})
        elif e.get("set") and e.get("number"):
            to_lookup.append({"set": e["set"], "collector_number": e["number"]})
        else:
            to_lookup.append({"name": e["name"]})

    resolved_map: dict[str, dict[str, Any]] = {}
    meld_result_ids: set[str] = set()

    def index_card(c: dict[str, Any]) -> None:
        resolved_map[c["id"]] = c
        resolved_map[f"{c.get('set', '')}:{c.get('collector_number', '')}"] = c
        keys = _name_keys(c.get("name", ""))
        resolved_map[keys[0]] = c
        for k in keys[1:]:
            resolved_map.setdefault(k, c)
        meld_result_ids.update(_meld_result_ids(c))

    for c in cached_cards.values():
        meld_result_ids.update(_meld_result_ids(c))

    if to_lookup:
        cards = await scryfall.collection(to_lookup)
        await upsert_printings(db, cards)
        for c in cards:
            index_card(c)
            _memo_name(c.get("name", ""), c["id"])

    # Pre-cachear las meld_result (bulk lookup) para que create_deck_from_entries
    # pueda añadirlas sin más queries. Solo las que no estén ya en local.
    if meld_result_ids:
        have = set(
            (
                await db.scalars(
                    select(PrintingCache.scryfall_id).where(
                        PrintingCache.scryfall_id.in_(meld_result_ids)
                    )
                )
            ).all()
        )
        need = [{"id": sfid} for sfid in meld_result_ids if sfid not in have]
        if need:
            await upsert_printings(db, await scryfall.collection(need))

    out: list[dict[str, Any]] = []
    for idx, e in enumerate(entries):
        card: dict[str, Any] | None = cached_cards.get(f"idx:{idx}")
        if not card and e.get("scryfall_id"):
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
    if cache_hits:
        log.info(
            "Import: %d/%d entradas resueltas desde caché local, %d consultadas a Scryfall",
            len(cache_hits), len(entries), len(to_lookup),
        )
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
        prefs_by_oracle = dict(rows)

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

    Pre-procesamiento con cache DFC (Extras · F1/T2):
    Antes de resolver contra Scryfall, cada entry con `name` se consulta
    contra el cache local de DFCPair. Si el usuario metió un nombre de
    reverso (ej. "Insectile Aberration", "Sage Animist"), lo revertimos al
    front correspondiente (Delver of Secrets, Nissa, Vastwood Seer) para
    que la impresión sea la correcta. Esto emula lo que hace MPC Autofill
    Desktop y ahorra al usuario horas de confusión.
    """
    entries = parse_plain_decklist(text)
    # El parser ya asigna roles según cabeceras `//Sideboard`, etc.
    # (state machine). Solo aplicamos default para entradas sin rol
    # explícito por retro-compatibilidad con parsers antiguos.
    for e in entries:
        e.setdefault("role", "mainboard")

    # DFC pre-processing: revertir backs a fronts usando el cache local.
    await _revert_dfc_backs_to_fronts(db, entries)

    resolved = await resolve_cards(db, scryfall, entries)
    unresolved = _unresolved_from_entries(resolved)
    deck = await create_deck_from_entries(
        db, name=name, entries=resolved, fmt=fmt, include_extras=include_extras,
    )
    return deck, unresolved


async def _revert_dfc_backs_to_fronts(
    db: AsyncSession, entries: list[dict[str, Any]],
) -> None:
    """Si `entries` contiene nombres que son BACKS de cartas DFC, los reemplaza
    por su FRONT usando el cache local ``dfc_pairs``.

    Sin esta corrección, "Insectile Aberration" (el reverso de Delver of
    Secrets) fallaría al resolver contra Scryfall porque no existe una
    printing con ese nombre principal. MPC Autofill Desktop hace lo mismo
    nativamente.

    Modifica ``entries`` in-place. Añade ``dfc_reverted_from`` para trazar el
    reemplazo (útil para logs y UI).

    No-op si el cache DFC está vacío (aún no sincronizado). En ese caso los
    entries pasan tal cual y Scryfall los rechazará como unresolved — el
    usuario los verá y sabrá corregir manualmente.
    """
    from sqlalchemy import func, select

    from mpc_forge.models import DFCPair

    # Recolectamos los names únicos (case-insensitive) que necesitamos verificar.
    names_by_lower: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        n = (e.get("name") or "").strip()
        if not n or e.get("scryfall_id"):
            continue
        # Si el nombre ya contiene " // " es un nombre DFC completo (ambas caras).
        # Enviarlo a Scryfall tal cual es correcto — NO intentar revertirlo,
        # porque sólo los nombres de BACK-face puro (sin //) son candidatos.
        # Revertir "Zanarkand, Ancient Metropolis // Lasting Fayth" podría
        # transformarlo erróneamente si el caché tiene datos inconsistentes.
        if " // " in n:
            continue
        names_by_lower.setdefault(n.lower(), []).append(e)
    if not names_by_lower:
        return

    # Buscar los que aparecen como BACK en el cache. Un solo query IN.
    rows = (await db.execute(
        select(DFCPair.front_name, DFCPair.back_name).where(
            func.lower(DFCPair.back_name).in_(list(names_by_lower.keys()))
        )
    )).all()

    revert_count = 0
    for front, back in rows:
        for entry in names_by_lower.get(back.lower(), []):
            original = entry["name"]
            entry["dfc_reverted_from"] = original
            entry["name"] = front
            revert_count += 1

    if revert_count > 0:
        # No es un error — el user tenía una lista con backs y los normalizamos.
        # Log en debug para no llenar la salida en imports masivos.
        import logging as _lg
        _lg.getLogger(__name__).info(
            "DFC pre-processing: revertidos %d backs a fronts vía cache local", revert_count,
        )


async def import_from_url(
    db: AsyncSession,
    scryfall: ScryfallClient,
    url: str,
    name: str | None = None,
    fmt: str = "commander",
    include_extras: bool = False,
) -> tuple[Deck, list[dict[str, Any]]]:
    """Import unificado desde cualquier sitio soportado.

    Detecta el sitio por hostname, descarga el mazo como texto plano vía el
    endpoint público correspondiente, y lo procesa por la pipeline común
    (parse_plain_decklist → resolve_cards → create_deck_from_entries).

    - ``name=None`` genera un nombre por defecto tipo "Moxfield · abc123" a
      partir del sitio detectado y el último segmento de la URL.

    Errores:
    - ``ValueError`` si el hostname no matchea ningún sitio soportado.
    - ``ImportSiteError`` si la descarga falla (URL inválida para el sitio,
      mazo privado, timeout, etc). El caller (route) lo mapea a HTTP 502.
    """
    from mpc_forge.clients.import_sites import resolve_site
    from mpc_forge.clients.import_sites.base import ImportSiteError

    site_cls = resolve_site(url)
    if site_cls is None:
        raise ValueError(
            f"URL no soportada. Hostname no reconocido: {url!r}. "
            f"Sitios soportados: ver /api/decks/import/supported-sites"
        )

    text = await site_cls.retrieve_card_list(url)
    if not text.strip():
        raise ImportSiteError(f"{site_cls.name} devolvió una lista vacía")

    if not name:
        # 1) Intentar obtener el nombre real del mazo desde el sitio.
        #    retrieve_deck_name() reutiliza el payload ya cacheado por
        #    retrieve_card_list (sin segundo fetch) cuando el sitio lo soporta.
        try:
            site_name = await site_cls.retrieve_deck_name(url)
        except Exception:
            site_name = None

        if site_name:
            name = site_name[:256]
        else:
            # 2) Fallback: autogenerar a partir del último segmento de la URL.
            #    Ej: https://www.moxfield.com/decks/AbCdEf → "Moxfield · AbCdEf"
            from urllib.parse import urlparse
            segments = [
                s for s in (urlparse(url).path or "").split("/") if s and s.lower() != "decks"
            ]
            tail = segments[-1] if segments else "imported"
            name = f"{site_cls.name} · {tail}"[:256]

    return await import_from_plaintext(
        db, scryfall,
        name=name, text=text, fmt=fmt,
        include_extras=include_extras,
    )


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
    except Exception as e:
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


# Oracle_ids por búsqueda al precargar. 15 mantiene la URL corta (~800
# caracteres) y acota lo que se pierde si una búsqueda falla.
PRINTS_BATCH_SIZE = 15


def _prints_known_complete(oracle_id: str) -> bool:
    at = _prints_complete.get(oracle_id)
    if at is None:
        return False
    if time.monotonic() - at > _PRINTS_MEMO_TTL:
        _prints_complete.pop(oracle_id, None)
        return False
    return True


async def fetch_printings_for_oracle(
    db: AsyncSession, scryfall: ScryfallClient, oracle_id: str
) -> list[PrintingCache]:
    """Devuelve todas las impresiones. Si aún no las tenemos, las bajamos.

    Se considera que el caché local ya tiene todas las impresiones si:
      * ya se descargaron en este proceso (aunque la carta tenga una sola
        impresión — antes esas se volvían a pedir cada vez que se abría el
        mazo), o
      * el usuario importó el volcado completo de Scryfall, o
      * hay al menos dos impresiones en inglés (heurística previa; las filas
        localizadas no cuentan porque llegan de una en una al traducir cartas).
    """
    stmt = (
        select(PrintingCache)
        .where(PrintingCache.oracle_id == oracle_id)
        .order_by(PrintingCache.released_at)
    )
    rows = list((await db.scalars(stmt)).all())
    if _prints_known_complete(oracle_id):
        return rows
    english = sum(1 for r in rows if (r.lang or "en") == "en")
    if english >= 2 or (rows and await _bulk_synced(db)):
        return rows

    prints = await scryfall.prints_by_oracle_id(oracle_id)
    # Se memoriza también una respuesta vacía: si Scryfall no conoce ese
    # oracle_id, preguntar otra vez en cada apertura del mazo no lo arregla.
    _prints_complete[oracle_id] = time.monotonic()
    if prints:
        await upsert_printings(db, prints)
        await db.commit()
        rows = list((await db.scalars(stmt)).all())
    return rows


async def pending_print_oracles(db: AsyncSession, oracle_ids: Iterable[str]) -> list[str]:
    """De ``oracle_ids``, los que aún no tienen todas sus impresiones en local.

    Mismo criterio que :func:`fetch_printings_for_oracle`, pero con una sola
    consulta para todo el mazo.
    """
    ids = [o for o in dict.fromkeys(oracle_ids) if o and not _prints_known_complete(o)]
    if not ids:
        return []
    bulk = await _bulk_synced(db)
    counts: dict[str, list[int]] = {}
    for chunk in _chunks(ids):
        rows = await db.execute(
            select(PrintingCache.oracle_id, PrintingCache.lang)
            .where(PrintingCache.oracle_id.in_(chunk))
        )
        for oid, lang in rows:
            total_en = counts.setdefault(oid, [0, 0])
            total_en[0] += 1
            if (lang or "en") == "en":
                total_en[1] += 1
    pending = []
    for oid in ids:
        total, english = counts.get(oid, (0, 0))
        if english >= 2 or (total and bulk):
            continue
        pending.append(oid)
    return pending


async def store_prints(
    db: AsyncSession, oracle_ids: Iterable[str], prints: list[dict[str, Any]]
) -> None:
    """Guarda las impresiones descargadas y marca esos oracle_ids como completos."""
    now = time.monotonic()
    for oid in oracle_ids:
        _prints_complete[oid] = now
    if prints:
        await upsert_printings(db, prints)
        await db.commit()
