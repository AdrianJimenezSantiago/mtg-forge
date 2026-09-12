"""Tests de integración de los endpoints del Sprint 4.

Usan las fixtures existentes (`client`, `deck`) y por tanto la base de datos
real de test, porque lo que se prueba aquí es el cruce entre tablas: mazo con
colección, tema con mazo, snapshot con mazo.
"""
from __future__ import annotations

# ===========================================================================
# Planificador
# ===========================================================================

class TestPlannerEndpoints:
    async def test_tiers_are_exposed(self, client):
        r = await client.get("/api/planner/tiers")
        assert r.status_code == 200
        body = r.json()
        assert body["tiers"], "No hay tiers configurados"
        assert body["max_size"] >= body["tiers"][0]["size"]

    async def test_plan_requires_at_least_one_deck(self, client):
        r = await client.post("/api/planner/plan", json={"deck_ids": []})
        assert r.status_code == 400

    async def test_plan_returns_runs_and_totals(self, client, deck):
        r = await client.post("/api/planner/plan", json={"deck_ids": [deck["id"]]})
        assert r.status_code == 200
        body = r.json()
        assert body["runs"], "Un mazo con cartas debe producir al menos un pedido"
        assert body["totals"]["cards"] == 3
        assert body["totals"]["runs"] == 1

    async def test_plan_names_the_decks_in_each_run(self, client, deck):
        body = (await client.post(
            "/api/planner/plan", json={"deck_ids": [deck["id"]]}
        )).json()
        assert deck["id"] in body["runs"][0]["deck_ids"]
        assert body["runs"][0]["deck_names"]

    async def test_plan_includes_alternatives(self, client, deck):
        body = (await client.post(
            "/api/planner/plan", json={"deck_ids": [deck["id"]]}
        )).json()
        assert body["alternatives"]
        assert any(o.get("is_cheapest") for o in body["alternatives"])

    async def test_plan_reports_wasted_slots(self, client, deck):
        """Tres cartas en el tier mínimo dejan casi todo el pedido vacío."""
        body = (await client.post(
            "/api/planner/plan", json={"deck_ids": [deck["id"]]}
        )).json()
        assert body["totals"]["wasted_slots"] > 0
        assert body["filler"]["total_wasted_slots"] > 0

    async def test_unknown_deck_is_ignored_not_fatal(self, client, deck):
        """Un mazo borrado en otra pestaña no debe tumbar el planificador."""
        r = await client.post(
            "/api/planner/plan", json={"deck_ids": [deck["id"], 999999]}
        )
        assert r.status_code == 200
        assert len(r.json()["decks"]) == 1

    async def test_effective_unit_exceeds_nominal_when_half_empty(
        self, client, deck
    ):
        run = (await client.post(
            "/api/planner/plan", json={"deck_ids": [deck["id"]]}
        )).json()["runs"][0]
        assert run["effective_unit_usd"] > run["unit_usd"]

    async def test_compare_endpoint_validates_its_input(self, client):
        assert (await client.get("/api/planner/compare?total_cards=0")).status_code == 422
        assert (await client.get("/api/planner/compare?total_cards=500")).status_code == 200


# ===========================================================================
# Diff colección ↔ mazo
# ===========================================================================

class TestPrintNeeds:
    async def test_unknown_deck_is_404(self, client):
        assert (await client.get("/api/decks/999999/print-needs")).status_code == 404

    async def test_empty_collection_means_printing_everything(self, client, deck):
        body = (await client.get(f"/api/decks/{deck['id']}/print-needs")).json()
        assert body["totals"]["quantity"] == 3
        assert body["totals"]["owned"] == 0
        assert body["totals"]["needed"] == 3
        assert body["totals"]["coverage_percent"] == 0.0

    async def test_every_card_is_listed(self, client, deck):
        body = (await client.get(f"/api/decks/{deck['id']}/print-needs")).json()
        assert len(body["cards"]) == 3
        names = {c["name"] for c in body["cards"]}
        assert "Sol Ring" in names

    async def test_owning_a_card_removes_it_from_the_list(self, client, deck):
        """El cruce que no existía: marcar una carta como poseída debe
        reflejarse en lo que hay que imprimir."""
        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        sol_ring = next(c for c in cards if c["name"] == "Sol Ring")

        toggled = await client.post("/api/collection/toggle-owned", json={
            "scryfall_id": sol_ring["scryfall_id"],
            "oracle_id": sol_ring["oracle_id"],
            "name": sol_ring["name"],
            "set_code": sol_ring.get("set_code", "c21"),
        })
        assert toggled.status_code in (200, 201), toggled.text

        body = (await client.get(f"/api/decks/{deck['id']}/print-needs")).json()
        assert body["totals"]["owned"] == 1
        assert body["totals"]["needed"] == 2
        entry = next(c for c in body["cards"] if c["name"] == "Sol Ring")
        assert entry["fully_owned"] is True

    async def test_exact_mode_is_stricter_than_oracle(self, client, deck):
        oracle = (await client.get(
            f"/api/decks/{deck['id']}/print-needs?match_mode=oracle"
        )).json()
        exact = (await client.get(
            f"/api/decks/{deck['id']}/print-needs?match_mode=exact"
        )).json()
        assert exact["totals"]["owned"] <= oracle["totals"]["owned"]

    async def test_basics_can_be_excluded(self, client, deck):
        r = await client.get(
            f"/api/decks/{deck['id']}/print-needs?include_basics=false"
        )
        assert r.status_code == 200

    async def test_multi_deck_needs_requires_decks(self, client):
        r = await client.post("/api/planner/print-needs", json={"deck_ids": []})
        assert r.status_code == 400

    async def test_multi_deck_needs_summarises(self, client, deck):
        r = await client.post(
            "/api/planner/print-needs", json={"deck_ids": [deck["id"]]}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["summary"]["decks"] == 1
        assert body["summary"]["total_quantity"] == 3

    async def test_shared_collection_does_not_double_count(self, client, deck):
        """Si tienes un Sol Ring y dos mazos lo llevan, solo uno lo aprovecha.

        Sin este reparto la suma de "ya lo tengo" saldría optimista y el
        usuario pediría de menos.
        """
        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        sol_ring = next(c for c in cards if c["name"] == "Sol Ring")
        await client.post("/api/collection/toggle-owned", json={
            "scryfall_id": sol_ring["scryfall_id"],
            "oracle_id": sol_ring["oracle_id"],
            "name": sol_ring["name"],
            "set_code": sol_ring.get("set_code", "c21"),
        })

        second = (await client.post("/api/decks/import/text", json={
            "name": "Segundo mazo", "text": "1 Sol Ring", "format": "commander",
        })).json()["deck"]

        ids = [deck["id"], second["id"]]
        shared = (await client.post("/api/planner/print-needs", json={
            "deck_ids": ids, "shared_collection": True,
        })).json()
        independent = (await client.post("/api/planner/print-needs", json={
            "deck_ids": ids, "shared_collection": False,
        })).json()

        assert shared["summary"]["total_owned"] == 1, (
            "La copia poseída solo puede usarse en un mazo"
        )
        assert independent["summary"]["total_owned"] == 2, (
            "Evaluados por separado, ambos mazos cuentan la copia"
        )


# ===========================================================================
# Temas de arte
# ===========================================================================

class TestArtThemes:
    async def test_list_starts_empty(self, client):
        r = await client.get("/api/art-themes")
        assert r.status_code == 200
        assert r.json()["themes"] == []

    async def test_create_from_a_deck(self, client, deck):
        r = await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "Mi estilo retro",
            "only_customized": False,
        })
        assert r.status_code == 201, r.text
        theme = r.json()
        assert theme["name"] == "Mi estilo retro"
        assert theme["entry_count"] == 3

    async def test_only_customized_captures_nothing_on_a_fresh_deck(
        self, client, deck
    ):
        """Un mazo recién importado no tiene arte elegido a mano: guardar sus
        100 cartas haría que aplicar el tema pisara el mazo destino entero."""
        r = await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "Vacío", "only_customized": True,
        })
        assert r.json()["entry_count"] == 0

    async def test_create_from_unknown_deck_is_404(self, client):
        r = await client.post("/api/art-themes", json={
            "deck_id": 999999, "name": "X",
        })
        assert r.status_code == 404

    async def test_name_cannot_be_empty(self, client, deck):
        r = await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "",
        })
        assert r.status_code == 422

    async def test_get_includes_entries(self, client, deck):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "T", "only_customized": False,
        })).json()
        body = (await client.get(f"/api/art-themes/{created['id']}")).json()
        assert len(body["entries"]) == 3
        assert all(e["oracle_id"] for e in body["entries"])

    async def test_unknown_theme_is_404(self, client):
        assert (await client.get("/api/art-themes/999999")).status_code == 404

    async def test_rename(self, client, deck):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "Antes", "only_customized": False,
        })).json()
        r = await client.patch(
            f"/api/art-themes/{created['id']}", json={"name": "Después"}
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Después"

    async def test_delete_removes_it(self, client, deck):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "Temporal", "only_customized": False,
        })).json()
        assert (await client.delete(
            f"/api/art-themes/{created['id']}"
        )).status_code == 204
        assert (await client.get(
            f"/api/art-themes/{created['id']}"
        )).status_code == 404

    async def test_preview_does_not_modify_anything(self, client, deck):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "T", "only_customized": False,
        })).json()
        before = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]

        preview = await client.get(
            f"/api/art-themes/{created['id']}/preview/{deck['id']}"
        )
        assert preview.status_code == 200
        assert "would_change" in preview.json()

        after = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        assert [c["scryfall_id"] for c in before] == [
            c["scryfall_id"] for c in after
        ]

    async def test_applying_a_theme_to_its_own_deck_changes_nothing(
        self, client, deck
    ):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "T", "only_customized": False,
        })).json()
        r = await client.post(
            f"/api/art-themes/{created['id']}/apply",
            json={"deck_id": deck["id"]},
        )
        assert r.status_code == 200
        assert r.json()["changed"] == 0

    async def test_applying_creates_a_snapshot_by_default(self, client, deck):
        """Una operación masiva debe poder deshacerse de un clic."""
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "T", "only_customized": False,
        })).json()
        r = await client.post(
            f"/api/art-themes/{created['id']}/apply",
            json={"deck_id": deck["id"], "create_snapshot": True},
        )
        assert r.json()["snapshot"] is not None

    async def test_applying_to_unknown_deck_is_404(self, client, deck):
        created = (await client.post("/api/art-themes", json={
            "deck_id": deck["id"], "name": "T", "only_customized": False,
        })).json()
        r = await client.post(
            f"/api/art-themes/{created['id']}/apply",
            json={"deck_id": 999999, "create_snapshot": False},
        )
        assert r.status_code == 404


# ===========================================================================
# Snapshots
# ===========================================================================

class TestSnapshots:
    async def test_a_new_deck_has_no_snapshots(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/snapshots")
        assert r.status_code == 200
        assert r.json()["snapshots"] == []

    async def test_create_and_list(self, client, deck):
        r = await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "Antes del torneo"}
        )
        assert r.status_code == 201
        assert r.json()["card_count"] == 3

        listed = (await client.get(f"/api/decks/{deck['id']}/snapshots")).json()
        assert len(listed["snapshots"]) == 1
        assert listed["snapshots"][0]["label"] == "Antes del torneo"

    async def test_unlabelled_snapshots_get_a_date(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/snapshots", json={"label": ""})
        assert r.json()["label"], "Un snapshot sin nombre debe recibir uno"

    async def test_unknown_deck_is_404(self, client):
        r = await client.post("/api/decks/999999/snapshots", json={"label": "X"})
        assert r.status_code == 404

    async def test_restore_brings_back_removed_cards(self, client, deck):
        snapshot = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "Completo"}
        )).json()

        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        await client.delete(f"/api/decks/{deck['id']}/cards/{cards[0]['id']}")
        reduced = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        assert len(reduced) == 2

        r = await client.post(f"/api/snapshots/{snapshot['id']}/restore")
        assert r.status_code == 200
        restored = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        assert len(restored) == 3

    async def test_restoring_saves_the_current_state_first(self, client, deck):
        """Restaurar por error no debe ser un callejón sin salida."""
        snapshot = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "A"}
        )).json()
        await client.post(f"/api/snapshots/{snapshot['id']}/restore")

        listed = (await client.get(f"/api/decks/{deck['id']}/snapshots")).json()
        assert len(listed["snapshots"]) == 2
        assert any(s["auto"] for s in listed["snapshots"])

    async def test_diff_against_current_state_detects_a_removal(
        self, client, deck
    ):
        snapshot = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "A"}
        )).json()
        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        await client.delete(f"/api/decks/{deck['id']}/cards/{cards[0]['id']}")

        body = (await client.get(f"/api/snapshots/{snapshot['id']}/diff")).json()
        assert body["summary"]["removed"] == 1
        assert body["to"]["label"] == "Estado actual"

    async def test_diff_of_identical_states_is_empty(self, client, deck):
        snapshot = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "A"}
        )).json()
        body = (await client.get(f"/api/snapshots/{snapshot['id']}/diff")).json()
        assert body["changes"] == []

    async def test_diff_between_two_snapshots(self, client, deck):
        first = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "A"}
        )).json()
        cards = (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
        await client.delete(f"/api/decks/{deck['id']}/cards/{cards[0]['id']}")
        second = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "B"}
        )).json()

        body = (await client.get(
            f"/api/snapshots/{first['id']}/diff?against={second['id']}"
        )).json()
        assert body["summary"]["removed"] == 1
        assert body["to"]["label"] == "B"

    async def test_delete_snapshot(self, client, deck):
        snapshot = (await client.post(
            f"/api/decks/{deck['id']}/snapshots", json={"label": "X"}
        )).json()
        assert (await client.delete(
            f"/api/snapshots/{snapshot['id']}"
        )).status_code == 204
        listed = (await client.get(f"/api/decks/{deck['id']}/snapshots")).json()
        assert listed["snapshots"] == []

    async def test_unknown_snapshot_operations_are_404(self, client):
        assert (await client.post(
            "/api/snapshots/999999/restore"
        )).status_code == 404
        assert (await client.get("/api/snapshots/999999/diff")).status_code == 404
        assert (await client.delete("/api/snapshots/999999")).status_code == 404
