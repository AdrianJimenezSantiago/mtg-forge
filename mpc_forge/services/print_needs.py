"""Diff entre un mazo y la colección: qué hay que imprimir de verdad.

El modelo tenía ``CollectionEntry`` y ``DeckCard`` desde hace tiempo, pero no
hablaban entre sí. El usuario podía marcar qué cartas posee y podía montar
mazos, y aun así la pregunta que se hace todo el que imprime proxies —"de estas
100, ¿cuántas tengo ya?"— no tenía respuesta en ninguna pantalla.

Dos formas de contar
--------------------
* **Por oracle_id** (por defecto). Si tienes Sol Ring en cualquier edición, no
  necesitas imprimirlo. Es lo que quiere casi todo el mundo: el proxy sustituye
  a la carta, y la edición da igual mientras se pueda jugar.
* **Por impresión exacta** (``exact``). Solo cuenta si posees ESA impresión
  concreta. Es para quien quiere un mazo visualmente homogéneo y considera que
  su Sol Ring de Commander 2013 no vale para el mazo bordeless.

Reparto entre varios mazos
--------------------------
Al planificar varios mazos a la vez aparece un matiz que es fácil pasar por
alto: si tienes UN Sol Ring y tres mazos lo llevan, solo uno puede usar el
original. Los otros dos necesitan proxy.

Con ``shared_collection=True`` las copias poseídas se reparten una sola vez
entre todos los mazos, en el orden en que se piden. Con ``False`` cada mazo se
evalúa por separado, que es lo correcto si vas a desmontar unos para montar
otros.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.models import CollectionEntry, Deck, DeckCard

log = logging.getLogger(__name__)

MatchMode = Literal["oracle", "exact"]

# Las tierras básicas se tratan aparte: nadie marca en su colección las 37
# llanuras que tiene sueltas en una caja, así que contarlas como "hay que
# imprimirlas" distorsionaría el total. Se informan por separado para que el
# usuario decida.
BASIC_LAND_NAMES = {
    "plains", "island", "swamp", "mountain", "forest", "wastes",
    "snow-covered plains", "snow-covered island", "snow-covered swamp",
    "snow-covered mountain", "snow-covered forest",
    # Nombres en español, por si el mazo se localizó.
    "llanura", "isla", "pantano", "montaña", "bosque",
}


@dataclass
class CardNeed:
    """Una carta del mazo y cuántas copias faltan."""
    card_id: int
    name: str
    oracle_id: str
    scryfall_id: str
    quantity: int
    owned: int
    role: str = "mainboard"
    is_basic_land: bool = False
    # Impresiones concretas que el usuario posee de esta carta. Permite a la
    # interfaz decir "la tienes, pero en otra edición".
    owned_printings: list[dict[str, str]] = field(default_factory=list)

    @property
    def needed(self) -> int:
        return max(0, self.quantity - self.owned)

    @property
    def fully_owned(self) -> bool:
        return self.needed == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "name": self.name,
            "oracle_id": self.oracle_id,
            "scryfall_id": self.scryfall_id,
            "quantity": self.quantity,
            "owned": self.owned,
            "needed": self.needed,
            "fully_owned": self.fully_owned,
            "role": self.role,
            "is_basic_land": self.is_basic_land,
            "owned_printings": self.owned_printings,
        }


@dataclass
class DeckNeeds:
    deck_id: int
    deck_name: str
    match_mode: MatchMode
    cards: list[CardNeed] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(c.quantity for c in self.cards)

    @property
    def total_owned(self) -> int:
        return sum(c.owned for c in self.cards)

    @property
    def total_needed(self) -> int:
        return sum(c.needed for c in self.cards)

    @property
    def needed_excluding_basics(self) -> int:
        return sum(c.needed for c in self.cards if not c.is_basic_land)

    @property
    def basics_needed(self) -> int:
        return sum(c.needed for c in self.cards if c.is_basic_land)

    @property
    def coverage_percent(self) -> float:
        if self.total_quantity <= 0:
            return 0.0
        return round(100 * self.total_owned / self.total_quantity, 1)

    def to_dict(self, *, include_cards: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "deck_id": self.deck_id,
            "deck_name": self.deck_name,
            "match_mode": self.match_mode,
            "totals": {
                "quantity": self.total_quantity,
                "owned": self.total_owned,
                "needed": self.total_needed,
                "needed_excluding_basics": self.needed_excluding_basics,
                "basics_needed": self.basics_needed,
                "coverage_percent": self.coverage_percent,
            },
        }
        if include_cards:
            payload["cards"] = [c.to_dict() for c in self.cards]
        return payload


def is_basic_land(name: str) -> bool:
    """¿Es una tierra básica? Tolera prefijos de tipo y sufijos de variante."""
    normalized = name.strip().lower()
    # "Snow-Covered Forest // Forest" y similares: basta con la primera cara.
    normalized = normalized.split("//")[0].strip()
    return normalized in BASIC_LAND_NAMES


async def _load_collection_index(
    db: AsyncSession, match_mode: MatchMode
) -> tuple[dict[str, int], dict[str, list[dict[str, str]]]]:
    """Índice de la colección: cuántas copias hay por clave, y sus detalles.

    ``CollectionEntry`` tiene ``scryfall_id`` como clave primaria, es decir una
    fila por impresión poseída. Poseer dos copias de la misma impresión no se
    puede representar hoy; se cuenta como una. Es una limitación conocida del
    modelo, no de este cálculo.
    """
    rows = (await db.execute(select(CollectionEntry))).scalars().all()

    counts: dict[str, int] = defaultdict(int)
    details: dict[str, list[dict[str, str]]] = defaultdict(list)

    for entry in rows:
        key = entry.scryfall_id if match_mode == "exact" else entry.oracle_id
        counts[key] += 1
        # Los detalles se indexan SIEMPRE por oracle_id: aunque el modo sea
        # exacto, la interfaz quiere poder decir "la tienes en otra edición".
        details[entry.oracle_id].append({
            "scryfall_id": entry.scryfall_id,
            "set_code": entry.set_code,
            "set_name": entry.set_name,
            "collector_number": entry.collector_number,
        })

    return counts, details


async def compute_for_deck(
    db: AsyncSession,
    deck_id: int,
    *,
    match_mode: MatchMode = "oracle",
    include_basics: bool = True,
) -> DeckNeeds | None:
    """Qué falta por imprimir de un mazo. ``None`` si el mazo no existe."""
    deck = await db.get(Deck, deck_id)
    if deck is None:
        return None

    counts, details = await _load_collection_index(db, match_mode)
    result = await _compute(
        db, deck, counts, details,
        match_mode=match_mode, include_basics=include_basics,
    )
    return result


async def compute_for_decks(
    db: AsyncSession,
    deck_ids: list[int],
    *,
    match_mode: MatchMode = "oracle",
    include_basics: bool = True,
    shared_collection: bool = True,
) -> list[DeckNeeds]:
    """Igual que ``compute_for_deck`` pero para varios mazos.

    Con ``shared_collection`` las copias poseídas se consumen: si tienes un
    Sol Ring y tres mazos lo llevan, el primero lo usa y los otros dos lo
    necesitan impreso. Sin ese reparto, la suma de "necesito imprimir" saldría
    optimista y el usuario pediría de menos.
    """
    counts, details = await _load_collection_index(db, match_mode)
    # Copia mutable: si se comparte, se va descontando mazo a mazo.
    pool = dict(counts)

    results: list[DeckNeeds] = []
    for deck_id in deck_ids:
        deck = await db.get(Deck, deck_id)
        if deck is None:
            continue
        available = pool if shared_collection else dict(counts)
        needs = await _compute(
            db, deck, available, details,
            match_mode=match_mode, include_basics=include_basics,
            consume=shared_collection,
        )
        results.append(needs)
    return results


async def _compute(
    db: AsyncSession,
    deck: Deck,
    counts: dict[str, int],
    details: dict[str, list[dict[str, str]]],
    *,
    match_mode: MatchMode,
    include_basics: bool,
    consume: bool = False,
) -> DeckNeeds:
    cards = (await db.execute(
        select(DeckCard)
        .where(DeckCard.deck_id == deck.id, DeckCard.include.is_(True))
        .order_by(DeckCard.role, DeckCard.name)
    )).scalars().all()

    needs = DeckNeeds(deck_id=deck.id, deck_name=deck.name, match_mode=match_mode)

    for card in cards:
        basic = is_basic_land(card.name)
        if basic and not include_basics:
            continue

        key = card.scryfall_id if match_mode == "exact" else card.oracle_id
        available = counts.get(key, 0)
        # No se puede "poseer" más copias de las que el mazo pide.
        owned = min(available, card.quantity)

        if consume and owned > 0:
            counts[key] = available - owned

        needs.cards.append(CardNeed(
            card_id=card.id,
            name=card.name,
            oracle_id=card.oracle_id,
            scryfall_id=card.scryfall_id,
            quantity=card.quantity,
            owned=owned,
            role=card.role,
            is_basic_land=basic,
            owned_printings=details.get(card.oracle_id, []),
        ))

    return needs


def needs_summary(all_needs: list[DeckNeeds]) -> dict[str, Any]:
    """Agregado para la cabecera de la vista del planificador."""
    return {
        "decks": len(all_needs),
        "total_quantity": sum(n.total_quantity for n in all_needs),
        "total_owned": sum(n.total_owned for n in all_needs),
        "total_needed": sum(n.total_needed for n in all_needs),
        "needed_excluding_basics": sum(
            n.needed_excluding_basics for n in all_needs
        ),
        "basics_needed": sum(n.basics_needed for n in all_needs),
    }
