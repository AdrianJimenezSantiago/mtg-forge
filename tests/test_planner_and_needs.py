"""Tests del Sprint 4: planificador, diff de colección, temas y snapshots.

La lógica de coste y de reparto se prueba sin base de datos siempre que se
puede: son funciones puras sobre conteos y tiers, y probarlas directamente hace
que un fallo apunte al cálculo en vez de a la fixture.
"""
from __future__ import annotations

import pytest

from mpc_forge.services import print_needs, print_planner
from mpc_forge.services.print_planner import DeckContribution, RunPlan


def contribution(deck_id: int, count: int, name: str = "") -> DeckContribution:
    return DeckContribution(
        deck_id=deck_id,
        name=name or f"Mazo {deck_id}",
        card_count=count,
        distinct_cards=count,
    )


# ===========================================================================
# Planificador
# ===========================================================================

class TestTierSelection:
    def test_tiers_are_sorted_ascending(self):
        sizes = [int(t["size"]) for t in print_planner.tiers()]
        assert sizes == sorted(sizes)

    def test_exact_size_uses_that_tier(self):
        smallest = int(print_planner.tiers()[0]["size"])
        assert int(print_planner.smallest_tier_for(smallest)["size"]) == smallest

    def test_one_over_a_tier_jumps_to_the_next(self):
        sizes = [int(t["size"]) for t in print_planner.tiers()]
        if len(sizes) < 2:
            pytest.skip("Se necesitan al menos dos tiers")
        assert int(print_planner.smallest_tier_for(sizes[0] + 1)["size"]) == sizes[1]

    def test_above_the_maximum_returns_the_maximum(self):
        biggest = print_planner.max_tier_size()
        chosen = int(print_planner.smallest_tier_for(biggest * 10)["size"])
        assert chosen == biggest


class TestRunEconomics:
    """Un pedido se paga por tier, no por carta. Es el corazón del cálculo."""

    def test_you_pay_for_the_whole_tier(self):
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=100)
        assert run.subtotal_usd == pytest.approx(108 * 0.30, abs=0.01)

    def test_wasted_slots_are_the_gap(self):
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=100)
        assert run.wasted_slots == 8

    def test_a_full_run_wastes_nothing(self):
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=108)
        assert run.wasted_slots == 0
        assert run.fill_percent == 100.0

    def test_effective_unit_is_higher_than_the_nominal_one(self):
        """La cifra que compara de verdad: coste por carta ÚTIL."""
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=60)
        assert run.effective_unit_usd > run.unit_usd
        assert run.effective_unit_usd == pytest.approx(108 * 0.30 / 60, abs=0.001)

    def test_effective_unit_equals_nominal_when_full(self):
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=108)
        assert run.effective_unit_usd == pytest.approx(0.30, abs=0.001)

    def test_empty_run_does_not_divide_by_zero(self):
        run = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=0)
        assert run.effective_unit_usd == 0.0


class TestPlanRuns:
    def test_no_decks_gives_no_runs(self):
        assert print_planner.plan_runs([]) == []

    def test_empty_decks_are_ignored(self):
        assert print_planner.plan_runs([contribution(1, 0)]) == []

    def test_a_small_deck_fits_in_one_run(self):
        runs = print_planner.plan_runs([contribution(1, 100)])
        assert len(runs) == 1
        assert runs[0].card_count == 100

    def test_every_card_ends_up_in_some_run(self):
        decks = [contribution(i, 100) for i in range(1, 6)]
        runs = print_planner.plan_runs(decks)
        assert sum(r.card_count for r in runs) == 500

    def test_decks_stay_together_by_default(self):
        """Recibir medio mazo no sirve para jugar: los pedidos llegan por
        separado y con semanas de diferencia."""
        decks = [contribution(1, 300), contribution(2, 300), contribution(3, 300)]
        runs = print_planner.plan_runs(decks)
        for run in runs:
            # Ningún mazo aparece en dos pedidos distintos.
            assert len(run.deck_ids) == len(set(run.deck_ids))
        appearances = [d for r in runs for d in r.deck_ids]
        assert sorted(appearances) == [1, 2, 3]

    def test_a_deck_bigger_than_the_ceiling_is_split(self):
        """No hay alternativa: no cabe entero en ningún pedido."""
        oversized = print_planner.max_tier_size() + 200
        runs = print_planner.plan_runs([contribution(1, oversized)])
        assert len(runs) >= 2
        assert sum(r.card_count for r in runs) == oversized

    def test_volume_mode_fills_runs_tighter(self):
        decks = [contribution(i, 250) for i in range(1, 5)]
        together = print_planner.plan_runs(decks, keep_decks_together=True)
        by_volume = print_planner.plan_runs(decks, keep_decks_together=False)
        wasted_together = sum(r.wasted_slots for r in together)
        wasted_volume = sum(r.wasted_slots for r in by_volume)
        assert wasted_volume <= wasted_together, (
            "Ignorar los límites de mazo debe desperdiciar como mucho lo mismo"
        )

    def test_custom_ceiling_is_respected(self):
        runs = print_planner.plan_runs([contribution(1, 300)], max_tier=108)
        assert all(r.card_count <= 108 for r in runs)

    def test_ceiling_cannot_exceed_the_biggest_tier(self):
        """Un techo absurdo no debe inventar un tier que MPC no vende."""
        runs = print_planner.plan_runs([contribution(1, 5000)], max_tier=99999)
        assert all(r.tier_size <= print_planner.max_tier_size() for r in runs)

    def test_run_indices_are_sequential(self):
        decks = [contribution(i, 400) for i in range(1, 5)]
        runs = print_planner.plan_runs(decks)
        assert [r.index for r in runs] == list(range(len(runs)))


class TestAlternatives:
    def test_no_cards_gives_no_options(self):
        assert print_planner.compare_alternatives(0) == []

    def test_one_option_per_tier(self):
        options = print_planner.compare_alternatives(500)
        assert len(options) == len(print_planner.tiers())

    def test_sorted_by_real_cost_per_card(self):
        options = print_planner.compare_alternatives(500)
        units = [o["effective_unit_usd"] for o in options]
        assert units == sorted(units)

    def test_the_cheapest_is_flagged(self):
        options = print_planner.compare_alternatives(500)
        assert options[0].get("is_cheapest") is True
        assert sum(1 for o in options if o.get("is_cheapest")) == 1

    def test_every_option_covers_all_the_cards(self):
        total = 700
        for option in print_planner.compare_alternatives(total):
            assert option["runs"] * option["ceiling"] >= total

    def test_a_small_ceiling_needs_more_runs(self):
        options = {o["ceiling"]: o for o in print_planner.compare_alternatives(600)}
        sizes = sorted(options)
        assert options[sizes[0]]["runs"] >= options[sizes[-1]]["runs"]


class TestFillerSuggestions:
    def test_a_full_run_suggests_nothing(self):
        runs = [RunPlan(index=0, tier_size=108, unit_usd=0.3, card_count=108)]
        filler = print_planner.suggest_filler(runs)
        assert filler["total_wasted_slots"] == 0
        assert filler["per_run"] == []

    def test_wasted_slots_are_reported_as_free_cards(self):
        """Los huecos ya están pagados: llenarlos no cuesta nada más."""
        runs = [RunPlan(index=0, tier_size=108, unit_usd=0.3, card_count=90)]
        entry = print_planner.suggest_filler(runs)["per_run"][0]
        assert entry["wasted_slots"] == 18
        assert entry["free_cards_available"] == 18

    def test_downgrade_is_offered_when_it_saves_money(self):
        sizes = [int(t["size"]) for t in print_planner.tiers()]
        if len(sizes) < 2:
            pytest.skip("Se necesitan al menos dos tiers")
        # Justo una carta por encima del tier pequeño: quitar una baja de tier.
        tier = print_planner.smallest_tier_for(sizes[0] + 1)
        run = RunPlan(
            index=0, tier_size=int(tier["size"]),
            unit_usd=float(tier["unit_usd"]), card_count=sizes[0] + 1,
        )
        entry = print_planner.suggest_filler([run])["per_run"][0]
        downgrade = entry["downgrade_option"]
        assert downgrade is not None
        assert downgrade["remove_cards"] == 1
        assert downgrade["saving_usd"] > 0


# ===========================================================================
# Diff colección ↔ mazo
# ===========================================================================

class TestBasicLandDetection:
    @pytest.mark.parametrize("name", [
        "Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes",
        "Snow-Covered Forest", "plains", "  Mountain  ",
    ])
    def test_detects_basics(self, name):
        assert print_needs.is_basic_land(name)

    @pytest.mark.parametrize("name", [
        "Sol Ring", "Command Tower", "Ancient Tomb", "Plateau",
        "Prismatic Vista", "Forest Bear",
    ])
    def test_does_not_flag_other_cards(self, name):
        assert not print_needs.is_basic_land(name)

    def test_localized_names_are_recognized(self):
        """Un mazo localizado no debe romper el conteo de tierras."""
        assert print_needs.is_basic_land("Bosque")
        assert print_needs.is_basic_land("Montaña")

    def test_double_faced_uses_the_front(self):
        assert print_needs.is_basic_land("Forest // Forest")


class TestCardNeedArithmetic:
    def _need(self, quantity: int, owned: int) -> print_needs.CardNeed:
        return print_needs.CardNeed(
            card_id=1, name="Sol Ring", oracle_id="o", scryfall_id="s",
            quantity=quantity, owned=owned,
        )

    def test_owning_none_means_printing_all(self):
        assert self._need(4, 0).needed == 4

    def test_owning_all_means_printing_none(self):
        need = self._need(4, 4)
        assert need.needed == 0
        assert need.fully_owned

    def test_partial_ownership(self):
        assert self._need(4, 1).needed == 3

    def test_owning_more_than_needed_never_goes_negative(self):
        assert self._need(1, 5).needed == 0


class TestDeckNeedsAggregates:
    def _needs(self, cards) -> print_needs.DeckNeeds:
        return print_needs.DeckNeeds(
            deck_id=1, deck_name="Test", match_mode="oracle", cards=cards
        )

    def test_empty_deck_has_zero_coverage_without_dividing_by_zero(self):
        assert self._needs([]).coverage_percent == 0.0

    def test_coverage_is_a_percentage_of_owned_copies(self):
        needs = self._needs([
            print_needs.CardNeed(1, "A", "a", "s", quantity=2, owned=1),
            print_needs.CardNeed(2, "B", "b", "s", quantity=2, owned=2),
        ])
        assert needs.total_quantity == 4
        assert needs.total_owned == 3
        assert needs.coverage_percent == 75.0

    def test_basics_are_counted_separately(self):
        """Nadie marca en su colección las llanuras sueltas de una caja."""
        needs = self._needs([
            print_needs.CardNeed(1, "Sol Ring", "a", "s", quantity=1, owned=0),
            print_needs.CardNeed(
                2, "Forest", "b", "s", quantity=10, owned=0, is_basic_land=True
            ),
        ])
        assert needs.total_needed == 11
        assert needs.basics_needed == 10
        assert needs.needed_excluding_basics == 1


class TestNeedsSummary:
    def test_sums_across_decks(self):
        decks = [
            print_needs.DeckNeeds(
                deck_id=i, deck_name=f"D{i}", match_mode="oracle",
                cards=[print_needs.CardNeed(1, "A", "a", "s", 10, 4)],
            )
            for i in range(3)
        ]
        summary = print_needs.needs_summary(decks)
        assert summary["decks"] == 3
        assert summary["total_quantity"] == 30
        assert summary["total_owned"] == 12
        assert summary["total_needed"] == 18

    def test_empty_input_gives_zeros(self):
        summary = print_needs.needs_summary([])
        assert summary["decks"] == 0
        assert summary["total_needed"] == 0
