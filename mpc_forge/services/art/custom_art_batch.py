"""Asignar arte custom a todas las cartas de un mazo de una vez.

El flujo tiene dos pasos para que el usuario vea qué va a pasar antes de que
pase: `plan_*` empareja cartas e imágenes por nombre sin tocar nada, y `apply`
guarda solo las asignaciones que el usuario confirma. Las imágenes pueden venir
de la biblioteca ya indexada (carpeta de la app, carpetas vinculadas, un ZIP
importado) o ser una lista de nombres de fichero que el navegador aún no ha
subido.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CustomArt, Deck, DeckCard, PrintingCache
from mpc_forge.services.art import custom_art, thumbnails
from mpc_forge.services.art.name_matching import CardKeys, card_keys
from mpc_forge.services.decks import deck_activity, snapshots
from mpc_forge.services.decks.deck_activity import DeckActivityKind as K
from mpc_forge.utils.iterables import chunked

log = logging.getLogger(__name__)

Face = Literal["front", "back"]
Status = Literal["match", "same", "keep", "missing"]

DFC_LAYOUTS = frozenset({"transform", "modal_dfc", "double_faced_token", "reversible_card"})
MAX_FILENAMES = 2000
MAX_SUGGESTIONS = 3
SUGGESTION_CUTOFF = 0.72
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_ROLE_ORDER = {"commander": 0, "companion": 1, "mainboard": 2, "sideboard": 3, "maybeboard": 4}


class DeckNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Candidate:
    """Una imagen que puede ir en una carta: un arte indexado o un fichero."""

    ref: int
    key: str
    face: str
    variant: str | None
    filename: str
    thumb_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "filename": self.filename,
            "variant": self.variant,
            "face": self.face,
            "thumb_url": self.thumb_url,
        }


@dataclass
class PlanItem:
    deck_card_id: int
    name: str
    role: str
    quantity: int
    face: Face
    status: Status
    current_custom_art_id: int | None
    proposed: int | None
    candidates: list[Candidate]

    def to_dict(self) -> dict[str, Any]:
        return {
            "deck_card_id": self.deck_card_id,
            "name": self.name,
            "role": self.role,
            "quantity": self.quantity,
            "face": self.face,
            "status": self.status,
            "current_custom_art_id": self.current_custom_art_id,
            "proposed": self.proposed,
            "candidates": [c.to_dict() for c in self.candidates],
        }


@dataclass
class UnusedImage:
    candidate: Candidate
    suggestions: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {**self.candidate.to_dict(), "suggestions": self.suggestions}


@dataclass
class Plan:
    deck_id: int
    source: Literal["library", "files"]
    items: list[PlanItem] = field(default_factory=list)
    unused: list[UnusedImage] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.items:
            if item.face == "front":
                counts[item.status] += 1
            elif item.status == "match":
                counts["backs"] += 1
        return {
            "deck_id": self.deck_id,
            "source": self.source,
            "summary": {
                "cards": sum(1 for i in self.items if i.face == "front"),
                "match": counts["match"],
                "same": counts["same"],
                "keep": counts["keep"],
                "missing": counts["missing"],
                "backs": counts["backs"],
                "unused": len(self.unused),
                "rejected": len(self.rejected),
            },
            "items": [i.to_dict() for i in self.items],
            "unused": [u.to_dict() for u in self.unused],
            "rejected": self.rejected,
        }


@dataclass(frozen=True)
class _DeckCardInfo:
    card: DeckCard
    keys: CardKeys
    is_dfc: bool


async def _load_deck(db: AsyncSession, deck_id: int) -> list[_DeckCardInfo]:
    if await db.get(Deck, deck_id) is None:
        raise DeckNotFound(deck_id)
    cards = (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck_id))).all()
    printings: dict[str, PrintingCache] = {}
    for chunk in chunked({c.scryfall_id for c in cards if c.scryfall_id}):
        rows = await db.scalars(select(PrintingCache).where(PrintingCache.scryfall_id.in_(chunk)))
        printings.update({p.scryfall_id: p for p in rows})

    out: list[_DeckCardInfo] = []
    for card in sorted(cards, key=lambda c: (_ROLE_ORDER.get(c.role, 9), c.name.lower(), c.id)):
        printing = printings.get(card.scryfall_id)
        is_dfc = bool(printing and printing.layout in DFC_LAYOUTS)
        printed = printing.name if printing and printing.name != card.name else None
        back_name = printing.back_name if printing and is_dfc else None
        printed_dfc = f"{card.name.split(' // ')[0]} // {back_name}" if back_name else None
        out.append(_DeckCardInfo(card, card_keys(card.name, printed, printed_dfc), is_dfc))
    return out


def _sort_candidates(cands: Iterable[Candidate]) -> list[Candidate]:
    unique = {c.ref: c for c in cands}
    return sorted(unique.values(), key=lambda c: (c.variant is not None, c.filename.lower(), c.ref))


def _build_plan(
    deck_id: int,
    source: Literal["library", "files"],
    deck: list[_DeckCardInfo],
    candidates: Sequence[Candidate],
    *,
    overwrite: bool,
    report_unused: bool,
) -> Plan:
    index: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for cand in candidates:
        index[(cand.key, cand.face)].append(cand)

    def lookup(keys: Iterable[str], face: str) -> list[Candidate]:
        return [c for k in keys for c in index.get((k, face), ())]

    plan = Plan(deck_id=deck_id, source=source)
    used: set[int] = set()
    for info in deck:
        faces: list[tuple[Face, list[Candidate]]] = [
            ("front", _sort_candidates(lookup(info.keys.front, "front")))
        ]
        if info.is_dfc:
            back = lookup(info.keys.front, "back") + lookup(info.keys.back, "front")
            if back:
                faces.append(("back", _sort_candidates(back)))
        for face, cands in faces:
            current = (
                info.card.custom_art_front_id if face == "front" else info.card.custom_art_back_id
            )
            used.update(c.ref for c in cands)
            plan.items.append(_plan_item(info.card, face, cands, current, source, overwrite))

    if report_unused:
        plan.unused = _suggest(
            [c for c in candidates if c.ref not in used],
            {k: info.card.id for info in deck for k in info.keys.all},
        )
    return plan


def _plan_item(
    card: DeckCard,
    face: Face,
    cands: list[Candidate],
    current: int | None,
    source: str,
    overwrite: bool,
) -> PlanItem:
    status: Status
    proposed: int | None = None
    if not cands:
        status = "missing"
    elif source == "library" and current is not None and any(c.ref == current for c in cands):
        status, proposed = "same", current
    elif current is not None and not overwrite:
        status = "keep"
    else:
        status, proposed = "match", cands[0].ref
    return PlanItem(
        deck_card_id=card.id,
        name=card.name,
        role=card.role,
        quantity=card.quantity,
        face=face,
        status=status,
        current_custom_art_id=current,
        proposed=proposed,
        candidates=cands,
    )


def _suggest(unused: list[Candidate], card_by_key: dict[str, int]) -> list[UnusedImage]:
    """Cartas parecidas para cada imagen sin pareja (errores de tecleo, prefijos)."""
    keys = list(card_by_key)
    out: list[UnusedImage] = []
    for cand in unused:
        close = difflib.get_close_matches(
            cand.key, keys, n=MAX_SUGGESTIONS, cutoff=SUGGESTION_CUTOFF
        )
        if len(close) < MAX_SUGGESTIONS and len(cand.key) >= 4:
            close += [k for k in keys if k.startswith(cand.key) or cand.key.startswith(k)]
        ids = list(dict.fromkeys(card_by_key[k] for k in close))[:MAX_SUGGESTIONS]
        out.append(UnusedImage(cand, ids))
    return out


def _art_candidate(art: CustomArt) -> Candidate:
    return Candidate(
        ref=art.id,
        key=art.card_name_normalized,
        face=art.face,
        variant=art.variant_label,
        filename=art.filename,
        thumb_url=thumbnails.url_for_relative(art.relative_path),
    )


async def plan_from_library(
    db: AsyncSession,
    deck_id: int,
    *,
    overwrite: bool = False,
    art_ids: Sequence[int] | None = None,
) -> Plan:
    """Empareja el mazo con las imágenes ya indexadas.

    Con `art_ids` (las de un ZIP recién importado o una carpeta vinculada) se
    limita a esas imágenes y además informa de las que no encajan con ninguna
    carta, con sugerencias.
    """
    deck = await _load_deck(db, deck_id)
    if art_ids is not None:
        conditions = [CustomArt.id.in_(c) for c in chunked(set(art_ids))]
    else:
        keys = {k for info in deck for k in info.keys.all}
        conditions = [CustomArt.card_name_normalized.in_(c) for c in chunked(keys)]
    rows: list[CustomArt] = []
    for condition in conditions:
        rows.extend((await db.scalars(select(CustomArt).where(condition))).all())
    candidates = [_art_candidate(a) for a in rows]
    return await asyncio.to_thread(
        _build_plan,
        deck_id,
        "library",
        deck,
        candidates,
        overwrite=overwrite,
        report_unused=art_ids is not None,
    )


async def plan_from_filenames(
    db: AsyncSession,
    deck_id: int,
    filenames: Sequence[str],
    *,
    overwrite: bool = False,
) -> Plan:
    """Empareja el mazo con ficheros que el navegador todavía no ha subido.

    `ref` es la posición de cada fichero en `filenames`, para que el cliente
    sepa cuáles subir y para qué carta.
    """
    deck = await _load_deck(db, deck_id)
    candidates: list[Candidate] = []
    rejected: list[str] = []
    for i, raw in enumerate(filenames[:MAX_FILENAMES]):
        path = Path(raw.replace("\\", "/"))
        if path.suffix.lower() not in _IMAGE_EXTS or path.name.startswith("."):
            rejected.append(raw)
            continue
        key, face, variant = custom_art.parse_filename(path)
        candidates.append(Candidate(ref=i, key=key, face=face, variant=variant, filename=path.name))
    plan = await asyncio.to_thread(
        _build_plan, deck_id, "files", deck, candidates, overwrite=overwrite, report_unused=True
    )
    plan.rejected = rejected
    return plan


@dataclass(frozen=True)
class Assignment:
    deck_card_id: int
    face: Face
    custom_art_id: int


@dataclass
class ApplyResult:
    deck_id: int
    changed: int = 0
    unchanged: int = 0
    invalid: int = 0
    snapshot_id: int | None = None
    event_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))


async def apply(db: AsyncSession, deck_id: int, assignments: Sequence[Assignment]) -> ApplyResult:
    """Guarda las asignaciones confirmadas en un solo paso deshacible.

    Antes de cambiar nada guarda una snapshot automática del mazo y registra
    un único evento en el historial con el antes y el después de cada carta.
    """
    deck = await db.get(Deck, deck_id)
    if deck is None:
        raise DeckNotFound(deck_id)
    result = ApplyResult(deck_id=deck_id)

    cards = {
        c.id: c
        for c in (await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck_id))).all()
    }
    wanted_art_ids = {a.custom_art_id for a in assignments}
    arts: dict[int, CustomArt] = {}
    for chunk in chunked(wanted_art_ids):
        rows = await db.scalars(select(CustomArt).where(CustomArt.id.in_(chunk)))
        arts.update({a.id: a for a in rows})

    final: dict[tuple[int, Face], int] = {}
    for a in assignments:
        if a.deck_card_id not in cards or a.custom_art_id not in arts:
            result.invalid += 1
            continue
        final[(a.deck_card_id, a.face)] = a.custom_art_id

    changes: list[dict[str, Any]] = []
    for (card_id, face), art_id in final.items():
        card = cards[card_id]
        old = card.custom_art_front_id if face == "front" else card.custom_art_back_id
        if old == art_id:
            result.unchanged += 1
            continue
        changes.append(
            {
                "deck_card_id": card_id,
                "name": card.name,
                "face": face,
                "old": old,
                "new": art_id,
                "filename": arts[art_id].filename,
            }
        )
    if not changes:
        return result

    snapshot = await snapshots.create(
        db, deck_id, label="Antes de aplicar arte custom en lote", auto=True
    )
    result.snapshot_id = snapshot["id"] if snapshot else None

    for change in changes:
        card = cards[change["deck_card_id"]]
        if change["face"] == "front":
            card.custom_art_front_id = change["new"]
        else:
            card.custom_art_back_id = change["new"]
    result.changed = len(changes)

    event = await deck_activity.log_event(
        db,
        deck_id,
        K.CUSTOM_ART_APPLIED,
        payload={
            "count": len(changes),
            "snapshot_id": result.snapshot_id,
            "changes": changes,
        },
        summary=f"Arte custom aplicado a {len(changes)} cartas",
        deck_name=deck.name,
    )
    result.event_id = event.id if event else None
    await db.commit()
    log.info("Arte custom en lote: %d cambios en el mazo %d", len(changes), deck_id)
    return result
