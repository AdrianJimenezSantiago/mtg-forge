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

from mpc_forge import config as cfg
from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.config import DEFAULT_CARDBACK_NAME
from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.services.art import custom_art as custom_art_service
from mpc_forge.services.art.art_cache import ArtCache, Face
from mpc_forge.services.cards.printings import upsert_printing

log = logging.getLogger(__name__)


_DFC_LAYOUTS = {"transform", "modal_dfc", "double_faced_token", "reversible_card"}


@dataclass
class DeckCardResolved:
    name: str
    quantity: int
    scryfall_id: str
    front_path: Path
    back_path: Path | None = None
    back_name: str | None = None
    query: str = ""


@dataclass
class XMLBuildResult:
    xml_path: Path
    total_cards: int
    fronts_by_slot: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    import re

    text = text.replace("//", " ")
    text = text.replace("-", " ")
    text = "".join(c for c in text.lower() if c.isalnum() or c == " ")
    return re.sub(r" +", " ", text).strip()


def _meld_result_id(printing: PrintingCache) -> str | None:
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

            resolved.append(
                DeckCardResolved(
                    name=dc.name,
                    quantity=dc.quantity,
                    scryfall_id=dc.scryfall_id,
                    front_path=front_path,
                    back_path=back_path,
                    back_name=back_name,
                    query=_slug(dc.name),
                )
            )

        if on_progress is not None:
            try:
                on_progress(dc.name)
            except Exception as e:
                log.debug("Progress callback falló: %s", e)

    return resolved


async def plan_deck_slots(
    db: AsyncSession,
    deck: Deck,
) -> list[DeckCardResolved]:

    cards = (
        await db.scalars(
            select(DeckCard)
            .where(DeckCard.deck_id == deck.id, DeckCard.include.is_(True))
            .order_by(DeckCard.role, DeckCard.name)
        )
    ).all()
    out: list[DeckCardResolved] = []
    _placeholder = Path("")
    for dc in cards:
        pc = await db.get(PrintingCache, dc.scryfall_id) if dc.scryfall_id else None
        has_back = bool(pc and pc.back_name)
        out.append(
            DeckCardResolved(
                name=dc.name,
                quantity=dc.quantity,
                scryfall_id=dc.scryfall_id or "",
                front_path=_placeholder,
                back_path=_placeholder if has_back else None,
                back_name=pc.back_name if pc else None,
                query="",
            )
        )
    return out


def build_xml(
    cards: list[DeckCardResolved],
    output_path: Path,
    cardstock: str,
    foil: bool,
    cardback_path: Path | None,
    web_mode: bool = False,
) -> XMLBuildResult:
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
    slots_with_custom_back: set[int] = set()

    for c in cards:
        slots = list(range(slot_cursor, slot_cursor + c.quantity))
        slot_cursor += c.quantity
        slots_str = ",".join(str(s) for s in slots)
        for s in slots:
            slots_map.append(c.name)

        front_card = ET.SubElement(fronts_el, "card")
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

    if cardback_path:
        remaining = [s for s in range(slot_cursor) if s not in slots_with_custom_back]
        if remaining:
            back_card = ET.SubElement(backs_el, "card")
            ET.SubElement(back_card, "id").text = "" if web_mode else str(cardback_path)
            ET.SubElement(back_card, "slots").text = ",".join(str(s) for s in remaining)
            cb_name = cardback_path.name
            ET.SubElement(back_card, "name").text = cb_name
            ET.SubElement(back_card, "query").text = _slug(cardback_path.stem)
        if not web_mode:
            ET.SubElement(root, "cardback").text = str(cardback_path)

    pretty = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(pretty, encoding="utf-8")
    return XMLBuildResult(xml_path=output_path, total_cards=total, fronts_by_slot=slots_map)


def default_cardback_path() -> Path | None:
    for ext in (".png", ".jpg", ".jpeg"):
        candidate = cfg.PATHS.cardbacks_dir / f"{DEFAULT_CARDBACK_NAME}{ext}"
        if candidate.exists():
            return candidate.resolve()
    return None
