"""Generador de XML para el desktop client de MPC-Autofill.

Aprovechamos el soporte de rutas locales en el campo <id> (releases recientes),
así el cliente no tiene que ir a las Google Drives indexadas.

Formato mínimo del XML esperado por mpc-autofill:

<order>
  <details>
    <quantity>N</quantity>
    <bracket>...</bracket>       (opcional; el desktop lo calcula)
    <stock>(S30) Standard Smooth</stock>
    <foil>false</foil>
  </details>
  <fronts>
    <card>
      <id>C:\\ruta\\absoluta\\arte.png</id>
      <slots>0,1,2</slots>
      <name>Sol Ring</name>
      <query>sol ring</query>
    </card>
    ...
  </fronts>
  <backs>
    <card>
      <id>C:\\ruta\\reverso.png</id>
      <slots>4</slots>
      <name>Delver of Secrets</name>
      <query>delver of secrets</query>
    </card>
    ...
  </backs>
  <cardback>C:\\ruta\\default-back.png</cardback>
</order>
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import DEFAULT_CARDBACK_NAME, PATHS
from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.services import custom_art as custom_art_service
from mpc_forge.services.art_cache import ArtCache, Face
from mpc_forge.services.deck_service import upsert_printing

log = logging.getLogger(__name__)


_DFC_LAYOUTS = {"transform", "modal_dfc", "double_faced_token", "reversible_card"}


@dataclass
class DeckCardResolved:
    """Info por carta lista para renderizar en XML."""
    name: str
    quantity: int
    scryfall_id: str
    front_path: Path
    back_path: Path | None = None  # DFC
    back_name: str | None = None
    query: str = ""


@dataclass
class XMLBuildResult:
    xml_path: Path
    total_cards: int
    fronts_by_slot: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    import re
    # Normaliza separador DFC (//) → espacio (antes de limpiar el resto)
    text = text.replace("//", " ")
    # Guiones → espacio: Ex-SOLDIER → ex soldier, Master-at-Arms → master at arms
    text = text.replace("-", " ")
    # Elimina todo lo que no sea alfanumérico ni espacio
    text = "".join(c for c in text.lower() if c.isalnum() or c == " ")
    # Colapsa múltiples espacios consecutivos (deja solo uno)
    return re.sub(r" +", " ", text).strip()


def _meld_result_id(printing: PrintingCache) -> str | None:
    """Extrae el ``scryfall_id`` del meld_result del ``related_parts`` JSON."""
    if not printing.related_parts:
        return None
    try:
        related = json.loads(printing.related_parts)
    except (ValueError, TypeError):
        return None
    for part in related:
        if part.get("component") == "meld_result":
            return part.get("id")
    return None


async def resolve_deck_for_xml(
    db: AsyncSession,
    scryfall: ScryfallClient,
    art_cache: ArtCache,
    deck: Deck,
    on_progress: Callable[[str], None] | None = None,
) -> list[DeckCardResolved]:
    """Resuelve todas las cartas del mazo descargando artes faltantes.

    Estrategia (batch en 3 fases, era 1 fila cada vez):

      A. Precarga en 3 queries: ``DeckCard`` del mazo, ``CustomArt`` referenciados,
         y ``PrintingCache`` de las scryfall_ids implicadas.
      B. Rellena huecos (printings ausentes o meld_results no cacheados) con
         ``scryfall.by_id`` — solo se llama cuando hace falta.
      C. Delega en :meth:`ArtCache.ensure_many` la descarga paralela de todos
         los artes oficiales requeridos, con un único commit al final.

    ``on_progress(card_name)`` se llama tras terminar cada carta. Se usa desde
    los endpoints de build para actualizar el tracker de progreso — pasar
    ``None`` (default) desactiva el tracking.
    """
    cards = (
        await db.scalars(
            select(DeckCard)
            .where(DeckCard.deck_id == deck.id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()
    if not cards:
        return []

    custom_ids = {c.custom_art_front_id for c in cards if c.custom_art_front_id}
    custom_ids |= {c.custom_art_back_id for c in cards if c.custom_art_back_id}
    customs_by_id: dict[int, CustomArt] = {}
    if custom_ids:
        customs_by_id = {
            ca.id: ca
            for ca in (
                await db.scalars(select(CustomArt).where(CustomArt.id.in_(custom_ids)))
            ).all()
        }

    scryfall_ids = {c.scryfall_id for c in cards if c.scryfall_id}
    printings_by_id: dict[str, PrintingCache] = {}
    if scryfall_ids:
        printings_by_id = {
            p.scryfall_id: p
            for p in (
                await db.scalars(
                    select(PrintingCache).where(PrintingCache.scryfall_id.in_(scryfall_ids))
                )
            ).all()
        }

    missing_printings = [sfid for sfid in scryfall_ids if sfid not in printings_by_id]
    if missing_printings:
        for sfid in missing_printings:
            card = await scryfall.by_id(sfid)
            if card:
                printings_by_id[sfid] = await upsert_printing(db, card)
        await db.commit()

    meld_result_ids: dict[str, str] = {}
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        if printing and printing.layout == "meld" and not dc.custom_art_back_id:
            mrid = _meld_result_id(printing)
            if mrid:
                meld_result_ids[dc.scryfall_id] = mrid

    if meld_result_ids:
        meld_ids = set(meld_result_ids.values())
        missing_meld = [m for m in meld_ids if m not in printings_by_id]
        if missing_meld:
            for sfid in missing_meld:
                card = await scryfall.by_id(sfid)
                if card:
                    printings_by_id[sfid] = await upsert_printing(db, card)
            await db.commit()

    art_requests: list[tuple[str, Face]] = []
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        needs_official_front = not dc.custom_art_front_id or (
            dc.custom_art_front_id not in customs_by_id
        )
        if needs_official_front and dc.scryfall_id:
            art_requests.append((dc.scryfall_id, "front"))
        if dc.custom_art_back_id and dc.custom_art_back_id in customs_by_id:
            continue
        if printing and printing.layout in _DFC_LAYOUTS:
            art_requests.append((dc.scryfall_id, "back"))
        elif dc.scryfall_id in meld_result_ids:
            art_requests.append((meld_result_ids[dc.scryfall_id], "front"))

    art_map = await art_cache.ensure_many(db, art_requests)

    resolved: list[DeckCardResolved] = []
    for dc in cards:
        printing = printings_by_id.get(dc.scryfall_id)
        front_path: Path | None = None
        back_path: Path | None = None
        back_name: str | None = None

        if dc.custom_art_front_id:
            ca_front = customs_by_id.get(dc.custom_art_front_id)
            if ca_front:
                abs_p = custom_art_service.absolute_path(ca_front)
                if abs_p.exists():
                    front_path = abs_p
                else:
                    log.warning(
                        "custom_art %s no existe en disco, cayendo al oficial",
                        ca_front.relative_path,
                    )

        if dc.custom_art_back_id:
            ca_back = customs_by_id.get(dc.custom_art_back_id)
            if ca_back:
                abs_p = custom_art_service.absolute_path(ca_back)
                if abs_p.exists():
                    back_path = abs_p
                    back_name = ca_back.filename

        if front_path is None and dc.scryfall_id:
            la = art_map.get((dc.scryfall_id, "front"))
            if la:
                front_path = art_cache.absolute_path(la)

        if front_path is None:
            log.warning("No se pudo resolver frente para %s (%s)", dc.name, dc.scryfall_id)
        else:
            if back_path is None and printing and printing.layout in _DFC_LAYOUTS:
                la = art_map.get((dc.scryfall_id, "back"))
                if la:
                    back_path = art_cache.absolute_path(la)
                    back_name = printing.back_name
            elif back_path is None and dc.scryfall_id in meld_result_ids:
                mrid = meld_result_ids[dc.scryfall_id]
                la = art_map.get((mrid, "front"))
                if la:
                    back_path = art_cache.absolute_path(la)
                    mr = printings_by_id.get(mrid)
                    back_name = (mr.name if mr else "meld back") + " (meld)"

            resolved.append(DeckCardResolved(
                name=dc.name,
                quantity=dc.quantity,
                scryfall_id=dc.scryfall_id,
                front_path=front_path,
                back_path=back_path,
                back_name=back_name,
                query=_slug(dc.name),
            ))

        if on_progress is not None:
            try:
                on_progress(dc.name)
            except Exception as e:  # noqa: BLE001
                log.debug("Progress callback falló: %s", e)

    return resolved


async def plan_deck_slots(
    db: AsyncSession,
    deck: Deck,
) -> list[DeckCardResolved]:
    """Versión ligera de ``resolve_deck_for_xml`` que NO descarga arte.

    Uso: previews que solo necesitan saber cuántas cartas y qué reversos
    (para split de print runs). Los ``front_path``/``back_path`` van a
    placeholders vacíos — es correcto porque el caller no genera XML,
    solo cuenta slots.

    `back_path` se pone como placeholder no-None cuando la carta es DFC/MDFC
    (según el PrintingCache), así el split de print runs puede distinguirlas
    para colocar reversos correctos si se combinan con `build_split_xml`.
    """
    from mpc_forge.models import PrintingCache
    cards = (
        await db.scalars(
            select(DeckCard)
            .where(DeckCard.deck_id == deck.id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()
    out: list[DeckCardResolved] = []
    _placeholder = Path("")  # nunca se leerá — solo cuenta como "hay back"
    for dc in cards:
        # Consultamos el PrintingCache solo por `back_name` — el resto no
        # importa para el plan de slots.
        pc = await db.get(PrintingCache, dc.scryfall_id) if dc.scryfall_id else None
        has_back = bool(pc and pc.back_name)
        out.append(DeckCardResolved(
            name=dc.name, quantity=dc.quantity, scryfall_id=dc.scryfall_id or "",
            front_path=_placeholder,
            back_path=_placeholder if has_back else None,
            back_name=pc.back_name if pc else None,
            query="",
        ))
    return out


def build_xml(
    cards: list[DeckCardResolved],
    output_path: Path,
    cardstock: str,
    foil: bool,
    cardback_path: Path | None,
    web_mode: bool = False,
) -> XMLBuildResult:
    """Construye el XML final.

    Asigna slots correlativos y agrupa cartas iguales para minimizar entradas.

    Args:
        web_mode: Si True, el campo ``<id>`` se deja vacío en lugar de usar
            la ruta local del arte. Usar cuando el destino es mpcfill.com
            (web), que busca imágenes por ``<query>`` y no puede leer rutas
            locales de Windows. Si False (default), se incluyen las rutas
            locales para que el desktop client de MPC Autofill las lea
            directamente desde disco.

    Estructura `<backs>` (compatible con MPC Autofill desktop tool y mpcfill.com):
      - Cada carta con back propio (DFC/MDFC/meld/custom back) → su propio `<card>`
        con el/los slots que ocupa esa carta.
      - Todos los demás slots (cartas normales) → un único `<card>` que apunta al
        cardback global con la lista CSV de slots. Esto es explícito y evita
        ambigüedades con distintas versiones del tool.
      - `<cardback>` se mantiene como fallback global por si el tool ignora `<backs>`.

    Los DFCs consumen el mismo slot en `fronts` y `backs`.
    """
    root = ET.Element("order")
    details = ET.SubElement(root, "details")
    total = sum(c.quantity for c in cards)
    ET.SubElement(details, "quantity").text = str(total)
    ET.SubElement(details, "stock").text = cardstock
    ET.SubElement(details, "foil").text = "true" if foil else "false"

    fronts_el = ET.SubElement(root, "fronts")
    backs_el = ET.SubElement(root, "backs")

    slot_cursor = 0
    slots_map: list[str] = []
    slots_with_custom_back: set[int] = set()  # slots que YA tienen back propio

    for c in cards:
        slots = list(range(slot_cursor, slot_cursor + c.quantity))
        slot_cursor += c.quantity
        slots_str = ",".join(str(s) for s in slots)
        for s in slots:
            slots_map.append(c.name)

        front_card = ET.SubElement(fronts_el, "card")
        # web_mode=True → <id> vacío; mpcfill.com usará <query> para buscar.
        # web_mode=False → ruta local para el desktop client de MPC Autofill.
        ET.SubElement(front_card, "id").text = "" if web_mode else str(c.front_path)
        ET.SubElement(front_card, "slots").text = slots_str
        ET.SubElement(front_card, "name").text = c.name
        ET.SubElement(front_card, "query").text = c.query

        if c.back_path:
            back_card = ET.SubElement(backs_el, "card")
            ET.SubElement(back_card, "id").text = "" if web_mode else str(c.back_path)
            ET.SubElement(back_card, "slots").text = slots_str
            ET.SubElement(back_card, "name").text = c.back_name or c.name
            ET.SubElement(back_card, "query").text = _slug(c.back_name or c.name)
            slots_with_custom_back.update(slots)

    # --- Cardback global aplicado explícitamente a los slots restantes ---
    if cardback_path:
        remaining = [s for s in range(slot_cursor) if s not in slots_with_custom_back]
        if remaining:
            back_card = ET.SubElement(backs_el, "card")
            ET.SubElement(back_card, "id").text = "" if web_mode else str(cardback_path)
            ET.SubElement(back_card, "slots").text = ",".join(str(s) for s in remaining)
            cb_name = cardback_path.name
            ET.SubElement(back_card, "name").text = cb_name
            ET.SubElement(back_card, "query").text = _slug(cardback_path.stem)
        # Fallback global (por si el tool no lee <backs>)
        if not web_mode:
            ET.SubElement(root, "cardback").text = str(cardback_path)

    pretty = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(pretty, encoding="utf-8")
    return XMLBuildResult(xml_path=output_path, total_cards=total, fronts_by_slot=slots_map)


def default_cardback_path() -> Path | None:
    """Busca un cardback por defecto en el directorio de cardbacks."""
    for ext in (".png", ".jpg", ".jpeg"):
        candidate = PATHS.cardbacks_dir / f"{DEFAULT_CARDBACK_NAME}{ext}"
        if candidate.exists():
            return candidate.resolve()
    return None
