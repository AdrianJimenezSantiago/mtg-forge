from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from mpc_forge.models import DeckCard
from mpc_forge.routes.export import xml as export_xml
from mpc_forge.services.printing import calibration, print_needs, print_planner
from mpc_forge.services.printing.pdf_generator import _ImageReaderCache
from mpc_forge.services.printing.print_planner import DeckContribution, RunPlan
from mpc_forge.services.printing.print_runs import (
    split_into_runs,
    split_into_runs_optimized,
    summary_dict,
)
from mpc_forge.services.printing.xml_generator import DeckCardResolved, XMLBuildResult


def _contribution(deck_id: int, count: int) -> DeckContribution:
    return DeckContribution(
        deck_id=deck_id, name=f"Mazo {deck_id}", card_count=count, distinct_cards=count
    )


def _slot(name: str, quantity: int = 1, back: bool = False) -> DeckCardResolved:
    return DeckCardResolved(
        name=name,
        quantity=quantity,
        scryfall_id=f"sf-{name}",
        front_path=Path(f"/t/{name}.png"),
        back_path=Path(f"/t/{name}_back.png") if back else None,
        back_name=f"{name} back" if back else None,
        query=name.lower(),
    )


class TestCalibration:
    def test_offsets_counter_the_measured_drift(self):
        aligned = calibration.derive_offsets(0, 0)
        assert (aligned.back_offset_x_mm, aligned.back_offset_y_mm) == (0, 0)
        assert aligned.warning == "calibration_already_aligned"
        assert calibration.derive_offsets(0, 2.0, flip_edge="long").back_offset_y_mm == -2.0
        assert calibration.derive_offsets(0, -3.5, flip_edge="long").back_offset_y_mm == 3.5
        assert calibration.derive_offsets(2.0, 0, flip_edge="long").back_offset_x_mm == 2.0
        short = calibration.derive_offsets(2.0, 2.0, flip_edge="short")
        assert (short.back_offset_x_mm, short.back_offset_y_mm) == (-2.0, 2.0)
        assert (short.measured_x_mm, short.measured_y_mm, short.flip_edge) == (2.0, 2.0, "short")

        for flip in ("long", "short"):
            for x, y in ((1.0, 1.0), (-2.5, 3.0), (0.5, -0.5)):
                first = calibration.derive_offsets(x, y, flip_edge=flip)
                again = calibration.derive_offsets(
                    first.back_offset_x_mm, first.back_offset_y_mm, flip_edge=flip
                )
                assert again.back_offset_x_mm == pytest.approx(x, abs=0.01)
                assert again.back_offset_y_mm == pytest.approx(y, abs=0.01)

        rounded = calibration.derive_offsets(1.23456, -2.98765)
        assert (rounded.back_offset_x_mm, rounded.back_offset_y_mm) == (1.23, 2.99)
        assert calibration.derive_offsets(0, 25.0).warning == "calibration_offset_suspicious"
        assert calibration.derive_offsets(1.5, -2.0).warning is None

    def test_explanation(self):
        def explain(x, y, flip="long"):
            return calibration.explain(calibration.derive_offsets(x, y, flip_edge=flip))

        assert explain(2.0, 1.0)["needs_correction"] is True
        assert explain(0, 0)["needs_correction"] is False
        assert explain(0.02, 0.0)["needs_correction"] is False
        assert explain(1, 1, "long")["explanation_key"] != explain(1, 1, "short")["explanation_key"]

    def test_sheet(self, tmp_path):
        path = calibration.build_sheet(tmp_path / "no" / "existe" / "cal.pdf")
        content = path.read_bytes()
        assert content.startswith(b"%PDF") and content.count(b"/Page") >= 2
        assert (
            calibration.build_sheet(tmp_path / "letter.pdf", page_size="letter").stat().st_size
            > 1000
        )
        assert calibration.build_sheet(tmp_path / "weird.pdf", page_size="inventado").exists()

    async def test_endpoints(self, client):
        body = (
            await client.post(
                "/api/calibration/derive",
                json={"measured_x_mm": 2.0, "measured_y_mm": -1.5, "flip_edge": "long"},
            )
        ).json()
        assert (body["back_offset_x_mm"], body["back_offset_y_mm"], body["needs_correction"]) == (
            2.0,
            1.5,
            True,
        )
        for invalid in (
            {"measured_x_mm": 500, "measured_y_mm": 0},
            {"measured_x_mm": 1, "measured_y_mm": 1, "flip_edge": "diagonal"},
        ):
            assert (await client.post("/api/calibration/derive", json=invalid)).status_code == 422

        sheet = await client.get("/api/calibration/sheet")
        assert sheet.headers["content-type"] == "application/pdf" and sheet.content.startswith(
            b"%PDF"
        )
        assert "no-store" in sheet.headers.get("cache-control", "")
        assert (await client.get("/api/calibration/sheet?page_size=a0")).status_code == 400


class TestPlanner:
    def test_tier_selection_and_run_economics(self):
        sizes = [int(t["size"]) for t in print_planner.tiers()]
        assert sizes == sorted(sizes) and len(sizes) >= 2
        assert int(print_planner.smallest_tier_for(sizes[0])["size"]) == sizes[0]
        assert int(print_planner.smallest_tier_for(sizes[0] + 1)["size"]) == sizes[1]
        biggest = print_planner.max_tier_size()
        assert int(print_planner.smallest_tier_for(biggest * 10)["size"]) == biggest

        partial = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=100)
        assert (
            partial.subtotal_usd == pytest.approx(108 * 0.30, abs=0.01)
            and partial.wasted_slots == 8
        )
        half = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=60)
        assert half.effective_unit_usd == pytest.approx(108 * 0.30 / 60, abs=0.001)
        assert half.effective_unit_usd > half.unit_usd
        full = RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=108)
        assert (full.wasted_slots, full.fill_percent) == (0, 100.0)
        assert full.effective_unit_usd == pytest.approx(0.30, abs=0.001)
        assert (
            RunPlan(index=0, tier_size=108, unit_usd=0.30, card_count=0).effective_unit_usd == 0.0
        )

    def test_plan_runs(self):
        assert print_planner.plan_runs([]) == [] == print_planner.plan_runs([_contribution(1, 0)])
        [single] = print_planner.plan_runs([_contribution(1, 100)])
        assert single.card_count == 100
        assert (
            sum(
                r.card_count
                for r in print_planner.plan_runs([_contribution(i, 100) for i in range(1, 6)])
            )
            == 500
        )

        together = print_planner.plan_runs([_contribution(i, 300) for i in range(1, 4)])
        assert all(len(r.deck_ids) == len(set(r.deck_ids)) for r in together)
        assert sorted(d for r in together for d in r.deck_ids) == [1, 2, 3]

        oversized = print_planner.max_tier_size() + 200
        split = print_planner.plan_runs([_contribution(1, oversized)])
        assert len(split) >= 2 and sum(r.card_count for r in split) == oversized

        decks = [_contribution(i, 250) for i in range(1, 5)]
        wasted_together = sum(
            r.wasted_slots for r in print_planner.plan_runs(decks, keep_decks_together=True)
        )
        wasted_volume = sum(
            r.wasted_slots for r in print_planner.plan_runs(decks, keep_decks_together=False)
        )
        assert wasted_volume <= wasted_together

        assert all(
            r.card_count <= 108
            for r in print_planner.plan_runs([_contribution(1, 300)], max_tier=108)
        )
        capped = print_planner.plan_runs([_contribution(1, 5000)], max_tier=99999)
        assert all(r.tier_size <= print_planner.max_tier_size() for r in capped)
        runs = print_planner.plan_runs([_contribution(i, 400) for i in range(1, 5)])
        assert [r.index for r in runs] == list(range(len(runs)))

    def test_alternatives_and_filler(self):
        assert print_planner.compare_alternatives(0) == []
        options = print_planner.compare_alternatives(700)
        assert len(options) == len(print_planner.tiers())
        units = [o["effective_unit_usd"] for o in options]
        assert units == sorted(units) and options[0]["is_cheapest"] is True
        assert sum(1 for o in options if o.get("is_cheapest")) == 1
        assert all(o["runs"] * o["ceiling"] >= 700 for o in options)
        by_ceiling = {o["ceiling"]: o for o in print_planner.compare_alternatives(600)}
        assert by_ceiling[min(by_ceiling)]["runs"] >= by_ceiling[max(by_ceiling)]["runs"]

        full = print_planner.suggest_filler(
            [RunPlan(index=0, tier_size=108, unit_usd=0.3, card_count=108)]
        )
        assert (full["total_wasted_slots"], full["per_run"]) == (0, [])
        entry = print_planner.suggest_filler(
            [RunPlan(index=0, tier_size=108, unit_usd=0.3, card_count=90)]
        )["per_run"][0]
        assert entry["wasted_slots"] == entry["free_cards_available"] == 18

        smallest = int(print_planner.tiers()[0]["size"])
        tier = print_planner.smallest_tier_for(smallest + 1)
        run = RunPlan(
            index=0,
            tier_size=int(tier["size"]),
            unit_usd=float(tier["unit_usd"]),
            card_count=smallest + 1,
        )
        downgrade = print_planner.suggest_filler([run])["per_run"][0]["downgrade_option"]
        assert downgrade["remove_cards"] == 1 and downgrade["saving_usd"] > 0

    async def test_endpoints(self, client, deck):
        tiers = (await client.get("/api/planner/tiers")).json()
        assert tiers["tiers"] and tiers["max_size"] >= tiers["tiers"][0]["size"]
        assert (await client.post("/api/planner/plan", json={"deck_ids": []})).status_code == 400

        body = (
            await client.post("/api/planner/plan", json={"deck_ids": [deck["id"], 999999]})
        ).json()
        assert len(body["decks"]) == 1
        assert (body["totals"]["cards"], body["totals"]["runs"]) == (3, 1)
        run = body["runs"][0]
        assert deck["id"] in run["deck_ids"] and run["deck_names"]
        assert run["effective_unit_usd"] > run["unit_usd"]
        assert any(o.get("is_cheapest") for o in body["alternatives"])
        assert body["totals"]["wasted_slots"] > 0 and body["filler"]["total_wasted_slots"] > 0

        assert (await client.get("/api/planner/compare?total_cards=0")).status_code == 422
        assert (await client.get("/api/planner/compare?total_cards=500")).status_code == 200


class TestPrintNeeds:
    def test_basic_lands(self):
        basics = [
            "Plains",
            "Island",
            "Swamp",
            "Mountain",
            "Forest",
            "Wastes",
            "Snow-Covered Forest",
            "plains",
            "  Mountain  ",
            "Bosque",
            "Montaña",
            "Forest // Forest",
        ]
        others = [
            "Sol Ring",
            "Command Tower",
            "Ancient Tomb",
            "Plateau",
            "Prismatic Vista",
            "Forest Bear",
        ]
        assert [n for n in basics if not print_needs.is_basic_land(n)] == []
        assert [n for n in others if print_needs.is_basic_land(n)] == []

    def test_arithmetic_and_aggregates(self):
        def need(quantity, owned, **kw):
            return print_needs.CardNeed(
                1, "Sol Ring", "o", "s", quantity=quantity, owned=owned, **kw
            )

        assert [need(4, owned).needed for owned in (0, 1, 4)] == [4, 3, 0]
        assert need(4, 4).fully_owned and need(1, 5).needed == 0

        def deck_needs(cards, deck_id=1):
            return print_needs.DeckNeeds(
                deck_id=deck_id, deck_name="T", match_mode="oracle", cards=cards
            )

        assert deck_needs([]).coverage_percent == 0.0
        coverage = deck_needs([need(2, 1), need(2, 2)])
        assert (coverage.total_quantity, coverage.total_owned, coverage.coverage_percent) == (
            4,
            3,
            75.0,
        )
        basics = deck_needs(
            [
                need(1, 0),
                print_needs.CardNeed(
                    2, "Forest", "b", "s", quantity=10, owned=0, is_basic_land=True
                ),
            ]
        )
        assert (basics.total_needed, basics.basics_needed, basics.needed_excluding_basics) == (
            11,
            10,
            1,
        )

        summary = print_needs.needs_summary(
            [deck_needs([need(10, 4)], deck_id=i) for i in range(3)]
        )
        assert (
            summary["decks"],
            summary["total_quantity"],
            summary["total_owned"],
            summary["total_needed"],
        ) == (3, 30, 12, 18)
        empty = print_needs.needs_summary([])
        assert (empty["decks"], empty["total_needed"]) == (0, 0)

    async def test_deck_needs_follow_the_collection(self, client, deck):
        url = f"/api/decks/{deck['id']}/print-needs"
        assert (await client.get("/api/decks/999999/print-needs")).status_code == 404
        body = (await client.get(url)).json()
        assert body["totals"] == {
            **body["totals"],
            "quantity": 3,
            "owned": 0,
            "needed": 3,
            "coverage_percent": 0.0,
        }
        assert len(body["cards"]) == 3 and "Sol Ring" in {c["name"] for c in body["cards"]}
        assert (await client.get(f"{url}?include_basics=false")).status_code == 200

        sol = next(
            c
            for c in (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
            if c["name"] == "Sol Ring"
        )
        toggled = await client.post(
            "/api/collection/toggle-owned",
            json={
                "scryfall_id": sol["scryfall_id"],
                "oracle_id": sol["oracle_id"],
                "name": sol["name"],
                "set_code": "c21",
            },
        )
        assert toggled.status_code in (200, 201)
        body = (await client.get(url)).json()
        assert (body["totals"]["owned"], body["totals"]["needed"]) == (1, 2)
        assert next(c for c in body["cards"] if c["name"] == "Sol Ring")["fully_owned"] is True
        exact = (await client.get(f"{url}?match_mode=exact")).json()
        assert exact["totals"]["owned"] <= body["totals"]["owned"]

        second = (
            await client.post(
                "/api/decks/import/text", json={"name": "Segundo", "text": "1 Sol Ring"}
            )
        ).json()["deck"]
        ids = [deck["id"], second["id"]]
        assert (
            await client.post("/api/planner/print-needs", json={"deck_ids": []})
        ).status_code == 400
        shared = (
            await client.post(
                "/api/planner/print-needs", json={"deck_ids": ids, "shared_collection": True}
            )
        ).json()
        independent = (
            await client.post(
                "/api/planner/print-needs", json={"deck_ids": ids, "shared_collection": False}
            )
        ).json()
        assert (shared["summary"]["decks"], shared["summary"]["total_quantity"]) == (2, 4)
        assert shared["summary"]["total_owned"] == 1 and independent["summary"]["total_owned"] == 2


class TestPrintRuns:
    def test_split_into_mpc_runs(self):
        empty = split_into_runs([])
        assert (empty.total_runs, empty.total_cards) == (0, 0)
        small = split_into_runs([_slot("X", 5)])
        assert (small.total_runs, small.runs[0].tier_size, small.runs[0].wasted_slots) == (
            1,
            18,
            13,
        )
        exact = split_into_runs([_slot(f"C{i}") for i in range(612)])
        assert (exact.total_runs, exact.runs[0].tier_size, exact.runs[0].wasted_slots) == (
            1,
            612,
            0,
        )
        over = split_into_runs([_slot(f"C{i}") for i in range(700)])
        assert (
            over.total_runs == 2
            and over.total_cards == sum(r.total_cards for r in over.runs) == 700
        )
        forced = split_into_runs([_slot(f"C{i}") for i in range(500)], max_tier=108)
        assert (
            all(r.tier_size <= 108 for r in forced.runs)
            and sum(r.total_cards for r in forced.runs) == 500
        )
        summary = summary_dict(split_into_runs([_slot(f"C{i}") for i in range(50)]))
        assert json.loads(json.dumps(summary))["total_runs"] == 1

    def test_high_quantity_cards_are_fragmented_with_their_back(self):
        basics = split_into_runs([_slot("Basic", 800)])
        assert (
            basics.total_runs >= 2 and sum(c.quantity for r in basics.runs for c in r.cards) == 800
        )
        delver = split_into_runs([_slot("Delver", 700, back=True)])
        assert delver.total_runs >= 2
        assert all(
            c.back_path is not None and c.back_name == "Delver back"
            for r in delver.runs
            for c in r.cards
        )

    def test_optimizer_never_wastes_more_than_greedy(self):
        cards = [_slot(f"C{i}") for i in range(620)]
        greedy, optimized = split_into_runs(cards), split_into_runs_optimized(cards)
        assert (
            sum(r.total_cards for r in greedy.runs)
            == sum(r.total_cards for r in optimized.runs)
            == 620
        )
        assert optimized.total_wasted_slots <= greedy.total_wasted_slots

    async def test_endpoints(self, client, deck, monkeypatch):
        preview = (await client.get(f"/api/decks/{deck['id']}/print-runs/preview")).json()
        assert preview["total_runs"] >= 1

        async def fake_resolve(db, scryfall, art_cache, deck_obj):
            cards = (
                await db.scalars(select(DeckCard).where(DeckCard.deck_id == deck_obj.id))
            ).all()
            return [
                DeckCardResolved(
                    name=c.name,
                    quantity=c.quantity,
                    scryfall_id=c.scryfall_id,
                    front_path=Path("front.png"),
                )
                for c in cards
            ]

        def fake_build(*, cards, output_path, **_):
            return XMLBuildResult(xml_path=output_path, total_cards=sum(c.quantity for c in cards))

        monkeypatch.setattr(export_xml, "resolve_deck_for_xml", fake_resolve)
        monkeypatch.setattr(export_xml, "build_xml", fake_build)
        r = await client.post(
            f"/api/decks/{deck['id']}/build-split-xml", json={"create_runs": True}
        )
        assert r.status_code == 200 and len(r.json()["run_ids"]) == 1


def test_pdf_image_reader_cache_decodes_each_file_once(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    first, second = tmp_path / "card.png", tmp_path / "other.png"
    Image.new("RGB", (100, 140), (20, 40, 80)).save(first)
    Image.new("RGB", (100, 140), (200, 10, 10)).save(second)

    assert "sin imágenes" in _ImageReaderCache().summary()
    cache = _ImageReaderCache()
    readers = [cache.get(str(first)) for _ in range(4)]
    assert all(r is readers[0] for r in readers)
    assert (cache.misses, cache.hits) == (1, 3)
    assert "1 imágenes únicas" in cache.summary() and "75%" in cache.summary()
    assert cache.get(str(second)) is not readers[0] and cache.misses == 2
