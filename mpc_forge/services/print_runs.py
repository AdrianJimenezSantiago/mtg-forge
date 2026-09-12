"""Split de un mazo en múltiples print runs para MakePlayingCards.

Motivación
----------
MPC tiene tiers de tirada fijos — no puedes pedir 100 cartas: eliges el
tier ≥100 (que es 108) y las 8 sobrantes van vacías o duplicadas. Si el
usuario quiere imprimir >612 cartas (varios mazos a la vez, un cubo…), es
imposible en un solo pedido y hay que dividir.

Este servicio recibe las cartas resueltas y devuelve la mejor partición
posible en 1..N print runs, cada uno con:

- Un tier oficial MPC.
- Las cartas concretas de ese run (preservando quantities).
- El coste unitario y total (integrado con cost_estimator).
- El cardback aplicable (mismo entre todos los runs por defecto).

Algoritmo
---------
Greedy first-fit-decreasing:

1. Ordenamos las cartas por cantidad descendente (menos fragmentación).
2. Empezamos con un run vacío del máximo tier (612).
3. Vamos añadiendo cartas al run actual mientras quepan.
4. Cuando una carta no cabe (con su quantity), abrimos un run nuevo.
5. Al final, cada run "encoge" al tier mínimo que contenga sus cartas.

Casos especiales
----------------
- **Cartas con quantity > tier máximo**: se dividen entre runs (ej. 700x
  Basic Land → 612 en run1 + 88 en run2). Es raro pero legal.
- **Cardback global**: siempre el mismo entre runs. Solo los backs de DFC
  varían por carta.
- **Total = 0**: devuelve lista vacía, no un run vacío.

Estrategia de padding
---------------------
Cuando el total no llega a un tier exacto (típico), MPC "rellena" los slots
sobrantes duplicando cartas. Aquí solo reportamos "wasted_slots" y dejamos
al usuario decidir si añadir cartas para llenar o pagar por slots vacíos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mpc_forge import config as cfg
from mpc_forge.services.xml_generator import DeckCardResolved


# Ordenamos los tiers una vez al import — MPC_TIERS puede cambiar en runtime
# si el usuario los edita en Ajustes, pero eso no es común.
def _get_tiers() -> list[dict[str, float]]:
    """Snapshot de los tiers actuales, ordenados por tamaño ascendente."""
    return sorted(cfg.MPC_TIERS, key=lambda t: int(t["size"]))


@dataclass
class PrintRunPlan:
    """Un pedido MPC concreto dentro de la partición.

    - ``cards`` conserva la misma estructura ``DeckCardResolved`` — el XML
      generator la consume directamente sin refactor.
    - ``total_cards`` = suma de quantities. Puede ser < ``tier_size``
      (padding lo cubre MPC duplicando cartas).
    """
    run_index: int
    cards: list[DeckCardResolved]
    tier_size: int
    unit_usd: float
    total_cards: int = 0
    wasted_slots: int = 0

    def __post_init__(self):
        self.total_cards = sum(c.quantity for c in self.cards)
        self.wasted_slots = max(0, self.tier_size - self.total_cards)

    @property
    def subtotal_usd(self) -> float:
        return round(self.tier_size * self.unit_usd, 2)


@dataclass
class SplitResult:
    """Salida de ``split_into_runs``. Contiene la lista de runs + agregados."""
    runs: list[PrintRunPlan] = field(default_factory=list)
    total_cards: int = 0
    total_runs: int = 0
    total_wasted_slots: int = 0
    total_subtotal_usd: float = 0.0

    def __post_init__(self):
        # __post_init__ solo se llama con los defaults; los agregados se
        # recalculan tras construir la lista completa via ``finalize()``.
        pass

    def finalize(self) -> SplitResult:
        self.total_runs = len(self.runs)
        self.total_cards = sum(r.total_cards for r in self.runs)
        self.total_wasted_slots = sum(r.wasted_slots for r in self.runs)
        self.total_subtotal_usd = round(sum(r.subtotal_usd for r in self.runs), 2)
        return self


def _tier_for(total: int) -> dict[str, float]:
    """Devuelve el tier oficial más pequeño que contiene ``total`` cartas.
    Si excede el máximo, devuelve el máximo (el caller debe haber dividido)."""
    tiers = _get_tiers()
    for t in tiers:
        if int(t["size"]) >= total:
            return t
    return tiers[-1]


def _max_tier_size() -> int:
    """Tamaño del mayor tier MPC (típicamente 612)."""
    return int(_get_tiers()[-1]["size"])


def split_into_runs(
    cards: list[DeckCardResolved],
    max_tier: int | None = None,
) -> SplitResult:
    """Divide las cartas resueltas en print runs de tamaño MPC válido.

    ``max_tier`` (opcional) permite forzar un techo por run (útil para probar
    "y si divido en runs de 108 aunque quepan más"). Por defecto usa el
    tier máximo definido en ``MPC_TIERS``.

    Devuelve un ``SplitResult`` con la partición y agregados. Si no hay
    cartas, devuelve un resultado vacío.
    """
    result = SplitResult()
    if not cards:
        return result.finalize()

    ceiling = max_tier if max_tier and max_tier > 0 else _max_tier_size()

    # Aplanamos: si una carta tiene qty > ceiling, la partimos en fragmentos
    # que quepan en un run. Preservamos back_path/etc — MPC nos verá copias
    # idénticas. Uso `list(cards)` para no mutar la lista original.
    expanded: list[DeckCardResolved] = []
    for c in cards:
        remaining = c.quantity
        while remaining > ceiling:
            expanded.append(DeckCardResolved(
                name=c.name, quantity=ceiling, scryfall_id=c.scryfall_id,
                front_path=c.front_path, back_path=c.back_path,
                back_name=c.back_name, query=c.query,
            ))
            remaining -= ceiling
        if remaining > 0:
            expanded.append(DeckCardResolved(
                name=c.name, quantity=remaining, scryfall_id=c.scryfall_id,
                front_path=c.front_path, back_path=c.back_path,
                back_name=c.back_name, query=c.query,
            ))

    # First-fit-decreasing: cartas grandes primero para minimizar
    # fragmentación. Mantenemos el orden original entre cartas del mismo
    # tamaño (importante para que los slots sean estables entre runs
    # sucesivos con el mismo mazo).
    expanded.sort(key=lambda c: (-c.quantity, c.name))

    current: list[DeckCardResolved] = []
    current_total = 0
    run_index = 0

    def _close_run():
        nonlocal current, current_total, run_index
        if not current:
            return
        tier = _tier_for(current_total)
        result.runs.append(PrintRunPlan(
            run_index=run_index,
            cards=list(current),
            tier_size=int(tier["size"]),
            unit_usd=float(tier["unit_usd"]),
        ))
        run_index += 1
        current = []
        current_total = 0

    for c in expanded:
        if current_total + c.quantity > ceiling:
            _close_run()
        current.append(c)
        current_total += c.quantity
    _close_run()

    return result.finalize()


def summary_dict(result: SplitResult) -> dict[str, Any]:
    """Serialización lista para API: dict friendly-JSON para el frontend."""
    return {
        "total_runs": result.total_runs,
        "total_cards": result.total_cards,
        "total_wasted_slots": result.total_wasted_slots,
        "total_subtotal_usd": result.total_subtotal_usd,
        "runs": [
            {
                "run_index": r.run_index,
                "tier_size": r.tier_size,
                "unit_usd": r.unit_usd,
                "subtotal_usd": r.subtotal_usd,
                "total_cards": r.total_cards,
                "wasted_slots": r.wasted_slots,
                "cards": [
                    {
                        "name": c.name,
                        "quantity": c.quantity,
                        "scryfall_id": c.scryfall_id,
                        "has_back": c.back_path is not None,
                    }
                    for c in r.cards
                ],
            }
            for r in result.runs
        ],
    }


# ---------------------------------------------------------------------------
# Solver DP: combinación óptima de tiers para minimizar wasted slots
# ---------------------------------------------------------------------------
# El greedy `split_into_runs` empaqueta al max_tier; en casos borderline
# como 620 (=612+8) puede ser preferible 486+180=666 (mismo total, más
# equilibrado) o incluso 396+234=630. Este solver DP encuentra la mejor
# combinación de tiers oficiales que suma >= total_cards y minimiza wasted.

def suggest_tier_combination(
    total_cards: int, max_runs: int = 8,
) -> list[int]:
    """Devuelve la combinación de tiers MPC oficiales que:
      1) Suma >= total_cards (necesario para caber todo).
      2) Minimiza los wasted_slots (diferencia con total_cards).
      3) Si hay empate en wasted, minimiza el número de runs.
      4) Si sigue habiendo empate, prefiere menor coste total.

    Devuelve una lista de tier_sizes (ej. [396, 234]) ordenada
    descendentemente. Si no cabe en `max_runs` tiers, devuelve la mejor
    aproximación posible.

    Trade-off: DP con hasta 8 runs es O(len(tiers)^max_runs * max_runs).
    Para MPC_TIERS con 11 valores y max_runs=8: ~10^8. Aceptable pero
    lento; usamos memoization + poda por total_cards.
    """
    tiers = sorted({int(t["size"]) for t in _get_tiers()})
    if not tiers or total_cards <= 0:
        return []

    # Costos unitarios para desempate por precio.
    unit_by_size = {int(t["size"]): float(t["unit_usd"]) for t in _get_tiers()}

    # Cache: (remaining, runs_left) → mejor combinación
    memo: dict[tuple[int, int], list[int]] = {}

    def _score(combo: list[int]) -> tuple[int, int, float]:
        # (wasted, num_runs, total_cost)
        s = sum(combo)
        wasted = s - total_cards
        cost = sum(x * unit_by_size[x] for x in combo)
        return (wasted, len(combo), cost)

    def _best_below(remaining: int, runs_left: int) -> list[int]:
        """Best combo que cubre `remaining` en <= runs_left runs."""
        if runs_left <= 0:
            return []
        # Si un solo tier grande basta, es candidato.
        # También podemos combinar 2+ para menor wasted.
        key = (remaining, runs_left)
        if key in memo:
            return memo[key]

        best: list[int] | None = None
        best_score: tuple[int, int, float] | None = None
        for t in tiers:
            if t >= remaining:
                # 1 solo run con este tier basta
                combo = [t]
                sc = _score(combo)
                if best is None or sc < best_score:
                    best, best_score = combo, sc
                # No hace falta seguir con este tier + subruns porque este
                # tier YA cabe todo. Pero combinaciones más pequeñas
                # pueden ganar en wasted, así que las exploramos también:
            # Explorar: usar este tier como head y combinar con subruns
            if runs_left > 1 and t < remaining:
                sub = _best_below(remaining - t, runs_left - 1)
                if sub:
                    combo = [t, *sub]
                    sc = _score(combo)
                    if best is None or sc < best_score:
                        best, best_score = combo, sc

        memo[key] = best or []
        return best or []

    combo = _best_below(total_cards, max_runs)
    return sorted(combo, reverse=True)


def split_into_runs_optimized(
    cards: list[DeckCardResolved],
    max_runs: int = 8,
) -> SplitResult:
    """Extras · F3/T10: variante de `split_into_runs` que usa DP para
    encontrar la combinación óptima de tiers antes de empaquetar.

    Delegación:
      1. Calcula el total de cartas.
      2. Llama a `suggest_tier_combination` para obtener los tier_sizes ideales.
      3. Empaqueta con first-fit-decreasing sobre esos tier_sizes (no sobre
         el max_tier global).

    Devuelve `SplitResult` con el mismo shape que `split_into_runs`.
    """
    if not cards:
        return SplitResult().finalize()

    total = sum(c.quantity for c in cards)
    tier_combo = suggest_tier_combination(total, max_runs=max_runs)
    if not tier_combo:
        # Fallback al greedy si el DP no encuentra nada (no debería pasar)
        return split_into_runs(cards)

    # Iterar tier_combo del más grande al más pequeño, empaquetando cartas.
    # Aplanamos duplicates (misma lógica que en split_into_runs original).
    max_size = max(tier_combo)
    expanded: list[DeckCardResolved] = []
    for c in cards:
        remaining = c.quantity
        while remaining > max_size:
            expanded.append(DeckCardResolved(
                name=c.name, quantity=max_size, scryfall_id=c.scryfall_id,
                front_path=c.front_path, back_path=c.back_path,
                back_name=c.back_name, query=c.query,
            ))
            remaining -= max_size
        if remaining > 0:
            expanded.append(DeckCardResolved(
                name=c.name, quantity=remaining, scryfall_id=c.scryfall_id,
                front_path=c.front_path, back_path=c.back_path,
                back_name=c.back_name, query=c.query,
            ))

    expanded.sort(key=lambda c: (-c.quantity, c.name))

    result = SplitResult()
    unit_by_size = {int(t["size"]): float(t["unit_usd"]) for t in _get_tiers()}

    for idx, tier_size in enumerate(tier_combo):
        current: list[DeckCardResolved] = []
        current_total = 0
        while expanded and current_total + expanded[0].quantity <= tier_size:
            c = expanded.pop(0)
            current.append(c)
            current_total += c.quantity
        if current:
            result.runs.append(PrintRunPlan(
                run_index=idx,
                cards=current,
                tier_size=tier_size,
                unit_usd=unit_by_size.get(tier_size, 0.0),
            ))

    # Si aún quedan cartas (el DP no anticipó una fragmentación adversa),
    # las metemos en run(s) adicionales usando el greedy.
    if expanded:
        remaining_plan = split_into_runs(expanded)
        for r in remaining_plan.runs:
            r.run_index = len(result.runs)
            result.runs.append(r)

    return result.finalize()
