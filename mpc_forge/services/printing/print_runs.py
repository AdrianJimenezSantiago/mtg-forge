from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mpc_forge import config as cfg
from mpc_forge.services.printing.xml_generator import DeckCardResolved


def _get_tiers() -> list[dict[str, float]]:
    return sorted(cfg.MPC_TIERS, key=lambda t: int(t["size"]))


@dataclass
class PrintRunPlan:
    run_index: int
    cards: list[DeckCardResolved]
    tier_size: int
    unit_usd: float
    total_cards: int = 0
    wasted_slots: int = 0

    def __post_init__(self) -> None:
        self.total_cards = sum(c.quantity for c in self.cards)
        self.wasted_slots = max(0, self.tier_size - self.total_cards)

    @property
    def subtotal_usd(self) -> float:
        return round(self.tier_size * self.unit_usd, 2)


@dataclass
class SplitResult:
    runs: list[PrintRunPlan] = field(default_factory=list)
    total_cards: int = 0
    total_runs: int = 0
    total_wasted_slots: int = 0
    total_subtotal_usd: float = 0.0

    def __post_init__(self) -> None:
        pass

    def finalize(self) -> SplitResult:
        self.total_runs = len(self.runs)
        self.total_cards = sum(r.total_cards for r in self.runs)
        self.total_wasted_slots = sum(r.wasted_slots for r in self.runs)
        self.total_subtotal_usd = round(sum(r.subtotal_usd for r in self.runs), 2)
        return self


def _tier_for(total: int) -> dict[str, float]:
    tiers = _get_tiers()
    for t in tiers:
        if int(t["size"]) >= total:
            return t
    return tiers[-1]


def _max_tier_size() -> int:
    return int(_get_tiers()[-1]["size"])


def split_into_runs(
    cards: list[DeckCardResolved],
    max_tier: int | None = None,
) -> SplitResult:
    result = SplitResult()
    if not cards:
        return result.finalize()

    ceiling = max_tier if max_tier and max_tier > 0 else _max_tier_size()

    expanded: list[DeckCardResolved] = []
    for c in cards:
        remaining = c.quantity
        while remaining > ceiling:
            expanded.append(
                DeckCardResolved(
                    name=c.name,
                    quantity=ceiling,
                    scryfall_id=c.scryfall_id,
                    front_path=c.front_path,
                    back_path=c.back_path,
                    back_name=c.back_name,
                    query=c.query,
                )
            )
            remaining -= ceiling
        if remaining > 0:
            expanded.append(
                DeckCardResolved(
                    name=c.name,
                    quantity=remaining,
                    scryfall_id=c.scryfall_id,
                    front_path=c.front_path,
                    back_path=c.back_path,
                    back_name=c.back_name,
                    query=c.query,
                )
            )

    expanded.sort(key=lambda c: (-c.quantity, c.name))

    current: list[DeckCardResolved] = []
    current_total = 0
    run_index = 0

    def _close_run() -> None:
        nonlocal current, current_total, run_index
        if not current:
            return
        tier = _tier_for(current_total)
        result.runs.append(
            PrintRunPlan(
                run_index=run_index,
                cards=list(current),
                tier_size=int(tier["size"]),
                unit_usd=float(tier["unit_usd"]),
            )
        )
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


def suggest_tier_combination(
    total_cards: int,
    max_runs: int = 8,
) -> list[int]:
    tiers = sorted({int(t["size"]) for t in _get_tiers()})
    if not tiers or total_cards <= 0:
        return []

    unit_by_size = {int(t["size"]): float(t["unit_usd"]) for t in _get_tiers()}

    memo: dict[tuple[int, int], list[int]] = {}

    def _score(combo: list[int]) -> tuple[int, int, float]:
        s = sum(combo)
        wasted = s - total_cards
        cost = sum(x * unit_by_size[x] for x in combo)
        return (wasted, len(combo), cost)

    def _best_below(remaining: int, runs_left: int) -> list[int]:
        if runs_left <= 0:
            return []
        key = (remaining, runs_left)
        if key in memo:
            return memo[key]

        best: list[int] | None = None
        best_score: tuple[int, int, float] | None = None
        for t in tiers:
            if t >= remaining:
                combo = [t]
                sc = _score(combo)
                if best_score is None or sc < best_score:
                    best, best_score = combo, sc
            if runs_left > 1 and t < remaining:
                sub = _best_below(remaining - t, runs_left - 1)
                if sub:
                    combo = [t, *sub]
                    sc = _score(combo)
                    if best_score is None or sc < best_score:
                        best, best_score = combo, sc

        memo[key] = best or []
        return best or []

    combo = _best_below(total_cards, max_runs)
    return sorted(combo, reverse=True)


def split_into_runs_optimized(
    cards: list[DeckCardResolved],
    max_runs: int = 8,
) -> SplitResult:
    if not cards:
        return SplitResult().finalize()

    total = sum(c.quantity for c in cards)
    tier_combo = suggest_tier_combination(total, max_runs=max_runs)
    if not tier_combo:
        return split_into_runs(cards)

    max_size = max(tier_combo)
    expanded: list[DeckCardResolved] = []
    for c in cards:
        remaining = c.quantity
        while remaining > max_size:
            expanded.append(
                DeckCardResolved(
                    name=c.name,
                    quantity=max_size,
                    scryfall_id=c.scryfall_id,
                    front_path=c.front_path,
                    back_path=c.back_path,
                    back_name=c.back_name,
                    query=c.query,
                )
            )
            remaining -= max_size
        if remaining > 0:
            expanded.append(
                DeckCardResolved(
                    name=c.name,
                    quantity=remaining,
                    scryfall_id=c.scryfall_id,
                    front_path=c.front_path,
                    back_path=c.back_path,
                    back_name=c.back_name,
                    query=c.query,
                )
            )

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
            result.runs.append(
                PrintRunPlan(
                    run_index=idx,
                    cards=current,
                    tier_size=tier_size,
                    unit_usd=unit_by_size.get(tier_size, 0.0),
                )
            )

    if expanded:
        remaining_plan = split_into_runs(expanded)
        for r in remaining_plan.runs:
            r.run_index = len(result.runs)
            result.runs.append(r)

    return result.finalize()
