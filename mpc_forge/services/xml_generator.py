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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import DEFAULT_CARDBACK_NAME, PATHS
from mpc_forge.models import CustomArt, Deck, DeckCard, LocalArt, PrintingCache
from mpc_forge.services import custom_art as custom_art_service
from mpc_forge.services.art_cache import ArtCache
from mpc_forge.services.deck_service import upsert_printing

log = logging.getLogger(__name__)


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
    return "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()


async def _resolve_deckcard(
    db: AsyncSession,
    scryfall: ScryfallClient,
    art_cache: ArtCache,
    dc: DeckCard,
) -> DeckCardResolved | None:
    """Resuelve una entrada de deck a rutas concretas de disco.

    Prioridad para el frente: custom_art_front_id → arte de Scryfall.
    Prioridad para el reverso:
      1. custom_art_back_id (el usuario puso una imagen específica)
      2. Si la carta es DFC/transform/MDFC: el back oficial de Scryfall
      3. Si la carta es meld: la imagen del meld_result (Brisela para Bruna/Gisela).
         Nota: Scryfall solo tiene la imagen completa de Brisela, no las mitades
         separadas. Para las mitades exactas, el usuario debe añadir custom back
         (p.ej. "Brisela Top.png" y "Brisela Bottom.png" en custom_art/).
    """
    import json as _json

    front_path: Path | None = None
    back_path: Path | None = None
    back_name: str | None = None

    # 1) Frente custom
    if dc.custom_art_front_id:
        ca_front = await db.get(CustomArt, dc.custom_art_front_id)
        if ca_front:
            abs_p = custom_art_service.absolute_path(ca_front)
            if abs_p.exists():
                front_path = abs_p
            else:
                log.warning(
                    "custom_art %s no existe en disco, cayendo al oficial", ca_front.relative_path
                )

    # 2) Reverso custom (independiente del frente)
    if dc.custom_art_back_id:
        ca_back = await db.get(CustomArt, dc.custom_art_back_id)
        if ca_back:
            abs_p = custom_art_service.absolute_path(ca_back)
            if abs_p.exists():
                back_path = abs_p
                back_name = ca_back.filename

    # 3) Necesitamos PrintingCache para saber si es DFC/meld y para rellenar
    #    el frente oficial si no hay custom.
    printing = await db.get(PrintingCache, dc.scryfall_id)
    if not printing:
        card = await scryfall.by_id(dc.scryfall_id)
        if not card:
            log.warning("No se encontró printing para %s (%s)", dc.name, dc.scryfall_id)
            return None
        printing = await upsert_printing(db, card)
        await db.commit()

    is_dfc = printing.layout in {
        "transform", "modal_dfc", "double_faced_token", "reversible_card"
    }
    is_meld = printing.layout == "meld"

    # 4) Frente oficial si aún no está
    if front_path is None:
        front = await art_cache.ensure(db, dc.scryfall_id, face="front")
        if not front:
            log.warning("No se pudo descargar arte para %s (%s)", dc.name, dc.scryfall_id)
            return None
        front_path = art_cache.absolute_path(front)

    # 5) Reverso oficial: DFC/MDFC → back de la propia carta
    if is_dfc and back_path is None:
        back = await art_cache.ensure(db, dc.scryfall_id, face="back")
        if back:
            back_path = art_cache.absolute_path(back)
            back_name = printing.back_name

    # 6) Reverso para MELD: buscamos el meld_result en related_parts y
    #    usamos su imagen completa como back. No es ideal (MTG oficial usa las
    #    mitades separadas), pero es la mejor aproximación automática. El usuario
    #    puede sobreescribir con custom_art_back_id.
    if is_meld and back_path is None and printing.related_parts:
        try:
            related = _json.loads(printing.related_parts)
        except (ValueError, TypeError):
            related = []
        meld_result_id = next(
            (p["id"] for p in related if p.get("component") == "meld_result"),
            None,
        )
        if meld_result_id:
            back = await art_cache.ensure(db, meld_result_id, face="front")
            if back:
                back_path = art_cache.absolute_path(back)
                # Nombre del meld_result para el <name> del XML
                mr = await db.get(PrintingCache, meld_result_id)
                back_name = (mr.name if mr else "meld back") + " (meld)"

    return DeckCardResolved(
        name=dc.name,
        quantity=dc.quantity,
        scryfall_id=dc.scryfall_id,
        front_path=front_path,
        back_path=back_path,
        back_name=back_name,
        query=_slug(dc.name),
    )


async def resolve_deck_for_xml(
    db: AsyncSession,
    scryfall: ScryfallClient,
    art_cache: ArtCache,
    deck: Deck,
) -> list[DeckCardResolved]:
    cards = (
        await db.scalars(
            select(DeckCard)
            .where(DeckCard.deck_id == deck.id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()
    resolved: list[DeckCardResolved] = []
    for dc in cards:
        r = await _resolve_deckcard(db, scryfall, art_cache, dc)
        if r:
            resolved.append(r)
    return resolved


def build_xml(
    cards: list[DeckCardResolved],
    output_path: Path,
    cardstock: str,
    foil: bool,
    cardback_path: Path | None,
) -> XMLBuildResult:
    """Construye el XML final.

    Asigna slots correlativos y agrupa cartas iguales para minimizar entradas.

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
        ET.SubElement(front_card, "id").text = str(c.front_path)
        ET.SubElement(front_card, "slots").text = slots_str
        ET.SubElement(front_card, "name").text = c.name
        ET.SubElement(front_card, "query").text = c.query

        if c.back_path:
            back_card = ET.SubElement(backs_el, "card")
            ET.SubElement(back_card, "id").text = str(c.back_path)
            ET.SubElement(back_card, "slots").text = slots_str
            ET.SubElement(back_card, "name").text = c.back_name or c.name
            ET.SubElement(back_card, "query").text = _slug(c.back_name or c.name)
            slots_with_custom_back.update(slots)

    # --- Cardback global aplicado explícitamente a los slots restantes ---
    # Si hay cardback_path y hay slots sin back propio, generamos un <card>
    # en <backs> con esos slots agrupados (formato usado por mpcfill.com).
    if cardback_path:
        remaining = [s for s in range(slot_cursor) if s not in slots_with_custom_back]
        if remaining:
            back_card = ET.SubElement(backs_el, "card")
            ET.SubElement(back_card, "id").text = str(cardback_path)
            ET.SubElement(back_card, "slots").text = ",".join(str(s) for s in remaining)
            cb_name = cardback_path.name
            ET.SubElement(back_card, "name").text = cb_name
            ET.SubElement(back_card, "query").text = _slug(cardback_path.stem)
        # Fallback global (por si el tool no lee <backs>)
        ET.SubElement(root, "cardback").text = str(cardback_path)

    # Bonito para debug.
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
