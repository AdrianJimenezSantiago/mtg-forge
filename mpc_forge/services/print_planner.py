"""Planificador de tiradas: reparte varios mazos en pedidos de MakePlayingCards.

Qué resuelve
------------
``print_runs.py`` ya sabía partir UN mazo en tiers válidos, pero estaba
enterrado: no había forma de usarlo desde la interfaz y solo operaba sobre un
mazo ya resuelto (con las imágenes descargadas).

Quien imprime proxies casi nunca pide un mazo suelto. Junta tres o cuatro,
mira cuántas cartas suman, y el envío desde MPC a Europa hace que agrupar sea
la diferencia entre pagar 0,25 €/carta y 0,60 €/carta. La pregunta real es
"¿qué combinación de mazos me sale mejor y con qué relleno los huecos?".

Por qué no reutiliza ``resolve_deck_for_xml``
---------------------------------------------
Resolver un mazo descarga todas sus imágenes. Planificar solo necesita
CANTIDADES: nombres y cuántas copias. Obligar a descargar cientos de imágenes
para responder "¿cuánto me costaría?" haría la vista inservible. Este módulo
trabaja sobre conteos y no toca el disco ni la red.

Modelo de coste
---------------
MPC cobra por TIER, no por carta. Un pedido de 100 cartas se factura como 108,
y las 8 sobrantes se rellenan duplicando. De ahí los dos conceptos centrales:

* ``wasted_slots`` — huecos pagados y no aprovechados.
* ``effective_unit`` — coste real por carta útil, que es
  ``tier_size * unit / cartas_reales``. Es el número que de verdad compara
  opciones, y el que la interfaz debe destacar.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge import config as cfg
from mpc_forge.models import Deck, DeckCard

log = logging.getLogger(__name__)


def tiers() -> list[dict[str, float]]:
    """Tiers de MPC ordenados de menor a mayor.

    Se lee de ``cfg`` en cada llamada, no en el import: el usuario puede editar
    los precios en Ajustes y el planificador debe reflejarlo sin reiniciar.
    """
    return sorted(cfg.MPC_TIERS, key=lambda t: int(t["size"]))


def smallest_tier_for(total: int) -> dict[str, float]:
    """El tier más pequeño donde caben ``total`` cartas."""
    for tier in tiers():
        if int(tier["size"]) >= total:
            return tier
    return tiers()[-1]


def max_tier_size() -> int:
    return int(tiers()[-1]["size"])


# ---------------------------------------------------------------------------
# Estructuras
# ---------------------------------------------------------------------------

@dataclass
class DeckContribution:
    """Lo que aporta un mazo al total. Solo conteos, sin imágenes."""
    deck_id: int
    name: str
    card_count: int          # suma de cantidades de las cartas incluidas
    distinct_cards: int
    excluded_count: int = 0  # cartas con include=False, informativo

    def to_dict(self) -> dict[str, Any]:
        return {
            "deck_id": self.deck_id,
            "name": self.name,
            "card_count": self.card_count,
            "distinct_cards": self.distinct_cards,
            "excluded_count": self.excluded_count,
        }


@dataclass
class RunPlan:
    """Un pedido concreto dentro del plan."""
    index: int
    tier_size: int
    unit_usd: float
    card_count: int
    deck_ids: list[int] = field(default_factory=list)
    deck_names: list[str] = field(default_factory=list)

    @property
    def wasted_slots(self) -> int:
        return max(0, self.tier_size - self.card_count)

    @property
    def subtotal_usd(self) -> float:
        # Se paga el tier completo, no las cartas.
        return round(self.tier_size * self.unit_usd, 2)

    @property
    def effective_unit_usd(self) -> float:
        """Coste real por carta útil. Es la cifra que compara de verdad."""
        if self.card_count <= 0:
            return 0.0
        return round(self.subtotal_usd / self.card_count, 4)

    @property
    def fill_percent(self) -> float:
        if self.tier_size <= 0:
            return 0.0
        return round(100 * self.card_count / self.tier_size, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tier_size": self.tier_size,
            "unit_usd": self.unit_usd,
            "card_count": self.card_count,
            "wasted_slots": self.wasted_slots,
            "subtotal_usd": self.subtotal_usd,
            "effective_unit_usd": self.effective_unit_usd,
            "fill_percent": self.fill_percent,
            "deck_ids": self.deck_ids,
            "deck_names": self.deck_names,
        }


@dataclass
class PlanResult:
    decks: list[DeckContribution] = field(default_factory=list)
    runs: list[RunPlan] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    filler: dict[str, Any] = field(default_factory=dict)

    @property
    def total_cards(self) -> int:
        return sum(r.card_count for r in self.runs)

    @property
    def total_wasted(self) -> int:
        return sum(r.wasted_slots for r in self.runs)

    @property
    def total_usd(self) -> float:
        return round(sum(r.subtotal_usd for r in self.runs), 2)

    @property
    def effective_unit_usd(self) -> float:
        if self.total_cards <= 0:
            return 0.0
        return round(self.total_usd / self.total_cards, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decks": [d.to_dict() for d in self.decks],
            "runs": [r.to_dict() for r in self.runs],
            "totals": {
                "cards": self.total_cards,
                "wasted_slots": self.total_wasted,
                "runs": len(self.runs),
                "subtotal_usd": self.total_usd,
                "effective_unit_usd": self.effective_unit_usd,
            },
            "alternatives": self.alternatives,
            "filler": self.filler,
        }


# ---------------------------------------------------------------------------
# Lectura de mazos
# ---------------------------------------------------------------------------

async def collect_contributions(
    db: AsyncSession, deck_ids: list[int]
) -> list[DeckContribution]:
    """Cuántas cartas aporta cada mazo, sin cargar las cartas en memoria.

    Se hace con agregados en SQL en vez de traer las filas: un cubo puede
    tener cientos de cartas por mazo y aquí solo interesan tres números.
    """
    if not deck_ids:
        return []

    names = dict(
        (await db.execute(
            select(Deck.id, Deck.name).where(Deck.id.in_(deck_ids))
        )).all()
    )

    rows = (await db.execute(
        select(
            DeckCard.deck_id,
            func.coalesce(func.sum(DeckCard.quantity), 0),
            func.count(DeckCard.id),
        )
        .where(DeckCard.deck_id.in_(deck_ids), DeckCard.include.is_(True))
        .group_by(DeckCard.deck_id)
    )).all()
    included = {r[0]: (int(r[1]), int(r[2])) for r in rows}

    excluded_rows = (await db.execute(
        select(DeckCard.deck_id, func.coalesce(func.sum(DeckCard.quantity), 0))
        .where(DeckCard.deck_id.in_(deck_ids), DeckCard.include.is_(False))
        .group_by(DeckCard.deck_id)
    )).all()
    excluded = {r[0]: int(r[1]) for r in excluded_rows}

    contributions = []
    # Se respeta el orden que pidió el usuario: la interfaz lista los mazos en
    # el orden en que los fue marcando y el plan debe coincidir.
    for deck_id in deck_ids:
        if deck_id not in names:
            continue
        count, distinct = included.get(deck_id, (0, 0))
        contributions.append(DeckContribution(
            deck_id=deck_id,
            name=names[deck_id],
            card_count=count,
            distinct_cards=distinct,
            excluded_count=excluded.get(deck_id, 0),
        ))
    return contributions


# ---------------------------------------------------------------------------
# Planificación
# ---------------------------------------------------------------------------

def plan_runs(
    contributions: list[DeckContribution],
    *,
    max_tier: int | None = None,
    keep_decks_together: bool = True,
) -> list[RunPlan]:
    """Reparte los mazos en pedidos.

    ``keep_decks_together`` (por defecto) no parte un mazo entre dos pedidos
    salvo que no quepa entero en ninguno. Es lo que quiere el usuario casi
    siempre: los pedidos de MPC llegan en semanas distintas, y recibir medio
    mazo no sirve para jugar.

    Con ``False`` se optimiza el llenado sin respetar los límites de mazo, que
    sale más barato por carta pero puede dejar un mazo repartido entre dos
    envíos.
    """
    ceiling = max_tier if max_tier and max_tier > 0 else max_tier_size()
    ceiling = min(ceiling, max_tier_size())

    pending = [c for c in contributions if c.card_count > 0]
    if not pending:
        return []

    if not keep_decks_together:
        return _plan_by_volume(pending, ceiling)

    # First-fit-decreasing sobre mazos completos: los grandes primero deja
    # menos fragmentación que ir en el orden en que el usuario los marcó.
    ordered = sorted(pending, key=lambda c: (-c.card_count, c.name))

    runs: list[RunPlan] = []
    buckets: list[list[DeckContribution]] = []
    loads: list[int] = []

    for deck in ordered:
        if deck.card_count > ceiling:
            # Un solo mazo más grande que el tier máximo: se parte, no hay
            # alternativa. Se reparte en trozos del tamaño del techo.
            remaining = deck.card_count
            while remaining > 0:
                chunk = min(remaining, ceiling)
                buckets.append([DeckContribution(
                    deck_id=deck.deck_id,
                    name=f"{deck.name} (parte {len(buckets) + 1})",
                    card_count=chunk,
                    distinct_cards=deck.distinct_cards,
                )])
                loads.append(chunk)
                remaining -= chunk
            continue

        placed = False
        for i, load in enumerate(loads):
            if load + deck.card_count <= ceiling:
                buckets[i].append(deck)
                loads[i] = load + deck.card_count
                placed = True
                break
        if not placed:
            buckets.append([deck])
            loads.append(deck.card_count)

    for index, (bucket, load) in enumerate(zip(buckets, loads, strict=False)):
        tier = smallest_tier_for(load)
        runs.append(RunPlan(
            index=index,
            tier_size=int(tier["size"]),
            unit_usd=float(tier["unit_usd"]),
            card_count=load,
            deck_ids=[d.deck_id for d in bucket],
            deck_names=[d.name for d in bucket],
        ))
    return runs


def _plan_by_volume(
    contributions: list[DeckContribution], ceiling: int
) -> list[RunPlan]:
    """Llena los pedidos al máximo sin respetar los límites de mazo."""
    total = sum(c.card_count for c in contributions)
    all_ids = [c.deck_id for c in contributions]
    all_names = [c.name for c in contributions]

    runs: list[RunPlan] = []
    index = 0
    remaining = total
    while remaining > 0:
        chunk = min(remaining, ceiling)
        tier = smallest_tier_for(chunk)
        runs.append(RunPlan(
            index=index,
            tier_size=int(tier["size"]),
            unit_usd=float(tier["unit_usd"]),
            card_count=chunk,
            deck_ids=all_ids,
            deck_names=all_names,
        ))
        remaining -= chunk
        index += 1
    return runs


def compare_alternatives(total_cards: int) -> list[dict[str, Any]]:
    """Coste de repartir ``total_cards`` con distintos techos por pedido.

    Sirve para responder "¿me sale mejor un pedido grande o dos pequeños?".
    Contra la intuición, dos pedidos medianos pueden salir más baratos por
    carta que uno grande medio vacío.
    """
    if total_cards <= 0:
        return []

    options = []
    for tier in tiers():
        ceiling = int(tier["size"])
        runs_needed = -(-total_cards // ceiling)   # división hacia arriba
        remaining = total_cards
        subtotal = 0.0
        wasted = 0
        for _ in range(runs_needed):
            chunk = min(remaining, ceiling)
            chunk_tier = smallest_tier_for(chunk)
            subtotal += int(chunk_tier["size"]) * float(chunk_tier["unit_usd"])
            wasted += max(0, int(chunk_tier["size"]) - chunk)
            remaining -= chunk
        options.append({
            "ceiling": ceiling,
            "runs": runs_needed,
            "subtotal_usd": round(subtotal, 2),
            "wasted_slots": wasted,
            "effective_unit_usd": round(subtotal / total_cards, 4),
        })

    # Se ordena por coste real por carta, que es lo que el usuario compara.
    options.sort(key=lambda o: o["effective_unit_usd"])
    if options:
        options[0]["is_cheapest"] = True
    return options


def suggest_filler(runs: list[RunPlan]) -> dict[str, Any]:
    """Qué hacer con los huecos pagados y no usados.

    MPC rellena los slots sobrantes duplicando cartas, así que esos huecos se
    pagan igual. Merece la pena aprovecharlos, y hay tres salidas razonables:
    subir el relleno con tierras básicas, bajar al tier anterior si al quitar
    unas pocas cartas se ahorra dinero, o meter cartas de otro mazo pendiente.
    """
    total_wasted = sum(r.wasted_slots for r in runs)
    per_run = []

    for run in runs:
        if run.wasted_slots <= 0:
            continue

        # ¿Bajar de tier compensa? Solo si sobran suficientes huecos como para
        # que el mazo entrara en el tier inmediatamente inferior.
        smaller = None
        for tier in tiers():
            if int(tier["size"]) < run.tier_size:
                smaller = tier
        downgrade = None
        if smaller is not None and run.card_count > int(smaller["size"]):
            excess = run.card_count - int(smaller["size"])
            saving = run.subtotal_usd - int(smaller["size"]) * float(smaller["unit_usd"])
            if saving > 0:
                downgrade = {
                    "tier_size": int(smaller["size"]),
                    "remove_cards": excess,
                    "saving_usd": round(saving, 2),
                }

        per_run.append({
            "run_index": run.index,
            "wasted_slots": run.wasted_slots,
            "fill_percent": run.fill_percent,
            # Los huecos ya están pagados: llenarlos con tierras básicas es
            # gratis en términos marginales.
            "free_cards_available": run.wasted_slots,
            "downgrade_option": downgrade,
        })

    return {
        "total_wasted_slots": total_wasted,
        "per_run": per_run,
        "advice_key": (
            "planner_filler_none" if total_wasted == 0
            else "planner_filler_available"
        ),
    }


async def build_plan(
    db: AsyncSession,
    deck_ids: list[int],
    *,
    max_tier: int | None = None,
    keep_decks_together: bool = True,
) -> PlanResult:
    """Plan completo para la vista ``/print-planner``."""
    contributions = await collect_contributions(db, deck_ids)
    runs = plan_runs(
        contributions,
        max_tier=max_tier,
        keep_decks_together=keep_decks_together,
    )
    total = sum(r.card_count for r in runs)
    return PlanResult(
        decks=contributions,
        runs=runs,
        alternatives=compare_alternatives(total),
        filler=suggest_filler(runs),
    )
