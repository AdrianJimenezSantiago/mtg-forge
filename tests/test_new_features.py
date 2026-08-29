"""Tests de las features de esta iteración: duplicar, buscar cartas,
deshacer eventos y progreso de build."""
from __future__ import annotations

import pytest


# =============================================================================
# Duplicar mazo
# =============================================================================

class TestDuplicateDeck:
    async def test_duplicate_copies_cards_and_metadata(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/duplicate", json={})
        assert r.status_code == 201, r.text
        new = r.json()

        # Nombre por defecto = "{original} (copia)"
        assert new["name"] == f"{deck['name']} (copia)"
        assert new["id"] != deck["id"]
        # Todas las cartas se copian con quantity/role
        assert len(new["cards"]) == len(deck["cards"])
        original_names = sorted(c["name"] for c in deck["cards"])
        new_names = sorted(c["name"] for c in new["cards"])
        assert original_names == new_names

    async def test_duplicate_with_custom_name(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/duplicate",
                              json={"name": "My Variant"})
        assert r.status_code == 201
        assert r.json()["name"] == "My Variant"

    async def test_duplicate_creates_activity_event(self, client, deck):
        r = await client.post(f"/api/decks/{deck['id']}/duplicate", json={})
        new = r.json()

        # Al nuevo mazo se le añade un deck_created con source: duplicated
        events = (await client.get(f"/api/decks/{new['id']}/activity")).json()
        assert len(events) == 1
        assert events[0]["kind"] == "deck_created"
        assert events[0]["payload"]["source"] == "duplicated"
        assert events[0]["payload"]["source_deck_id"] == deck["id"]

    async def test_duplicate_nonexistent_deck_returns_404(self, client):
        r = await client.post("/api/decks/99999/duplicate", json={})
        assert r.status_code == 404

    async def test_duplicate_with_empty_name_falls_back_to_default(self, client, deck):
        # Empty string → HTTP 400 (evita mazos sin nombre)
        r = await client.post(f"/api/decks/{deck['id']}/duplicate",
                              json={"name": "   "})
        assert r.status_code == 400


# =============================================================================
# Búsqueda global de cartas
# =============================================================================

class TestGlobalCardSearch:
    async def test_search_finds_cards_in_deck(self, client, deck):
        r = await client.get("/api/decks/_/search-cards?q=sol")
        assert r.status_code == 200
        data = r.json()
        assert data["total_groups"] >= 1
        names = [g["canonical_name"] for g in data["groups"]]
        assert "Sol Ring" in names

    async def test_search_groups_across_multiple_decks(self, client, deck):
        # Duplicamos el mazo → ahora Sol Ring está en 2 mazos
        await client.post(f"/api/decks/{deck['id']}/duplicate", json={})

        r = await client.get("/api/decks/_/search-cards?q=sol+ring")
        data = r.json()
        sol_ring_group = next(g for g in data["groups"] if g["canonical_name"] == "Sol Ring")
        assert sol_ring_group["deck_count"] == 2
        assert len(sol_ring_group["instances"]) == 2

    async def test_search_returns_thumbnail_and_printing_info(self, client, deck):
        r = await client.get("/api/decks/_/search-cards?q=sol")
        data = r.json()
        inst = data["groups"][0]["instances"][0]
        assert inst["deck_id"] == deck["id"]
        assert inst["role"] == "mainboard"
        assert inst["quantity"] == 1
        assert inst["printing_set"] == "c21"
        assert inst["printing_number"] == "263"
        assert inst["thumbnail"] is not None

    async def test_search_too_short_returns_empty(self, client, deck):
        r = await client.get("/api/decks/_/search-cards?q=s")
        data = r.json()
        assert data["total_groups"] == 0
        assert data["groups"] == []

    async def test_search_prefix_match_ranks_first(self, client, deck):
        # "sol" debería devolver "Sol Ring" antes que "Arcane Signet"
        r = await client.get("/api/decks/_/search-cards?q=sol")
        data = r.json()
        assert data["groups"][0]["canonical_name"] == "Sol Ring"

    async def test_search_case_insensitive(self, client, deck):
        r_lower = await client.get("/api/decks/_/search-cards?q=sol+ring")
        r_upper = await client.get("/api/decks/_/search-cards?q=SOL+RING")
        r_mixed = await client.get("/api/decks/_/search-cards?q=SoL+RiNg")
        assert r_lower.json()["total_groups"] == r_upper.json()["total_groups"]
        assert r_lower.json()["total_groups"] == r_mixed.json()["total_groups"]

    async def test_search_no_results_for_unknown_card(self, client, deck):
        r = await client.get("/api/decks/_/search-cards?q=xyzzyxxx")
        data = r.json()
        assert data["total_groups"] == 0


# =============================================================================
# Deshacer eventos
# =============================================================================

class TestUndo:
    async def test_undoable_kinds_endpoint(self, client):
        r = await client.get("/api/decks/_/undoable-kinds")
        assert r.status_code == 200
        kinds = r.json()
        # Los tipos "difíciles" (deck_created, role_cleared, localized) NO son reversibles.
        assert "card_moved" in kinds
        assert "card_added" in kinds
        assert "card_art_changed" in kinds
        assert "deck_renamed" in kinds
        assert "deck_created" not in kinds
        assert "role_cleared" not in kinds

    async def test_undo_card_moved(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        # Movemos Sol Ring a sideboard
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "sideboard"})

        # Buscamos el evento card_moved
        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        moved_event = next(e for e in events if e["kind"] == "card_moved")

        # Deshacer → Sol Ring debe volver a mainboard
        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{moved_event['id']}/undo"
        )
        assert r.status_code == 200
        assert "mainboard" in r.json()["summary"]

        # Verificamos el estado actual del mazo
        deck_after = (await client.get(f"/api/decks/{deck['id']}")).json()
        sol = next(c for c in deck_after["cards"] if c["name"] == "Sol Ring")
        assert sol["role"] == "mainboard"

    async def test_undo_deck_renamed(self, client, deck):
        await client.patch(f"/api/decks/{deck['id']}", json={"name": "New Name"})
        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        renamed = next(e for e in events if e["kind"] == "deck_renamed")

        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{renamed['id']}/undo"
        )
        assert r.status_code == 200

        deck_after = (await client.get(f"/api/decks/{deck['id']}")).json()
        assert deck_after["name"] == "Test Deck"  # nombre original

    async def test_undo_card_qty_changed(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Command Tower")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"quantity": 5})

        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        qty_event = next(e for e in events if e["kind"] == "card_qty_changed")

        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{qty_event['id']}/undo"
        )
        assert r.status_code == 200

        deck_after = (await client.get(f"/api/decks/{deck['id']}")).json()
        ct = next(c for c in deck_after["cards"] if c["name"] == "Command Tower")
        assert ct["quantity"] == 1

    async def test_undo_card_added_removes_the_card(self, client, deck):
        # Añadimos una carta nueva (Lightning Bolt no está en el deck base)
        r = await client.post(f"/api/decks/{deck['id']}/cards", json={
            "name": "Lightning Bolt", "quantity": 1, "role": "mainboard",
        })
        assert r.status_code == 201

        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        added = next(e for e in events if e["kind"] == "card_added")

        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{added['id']}/undo"
        )
        assert r.status_code == 200

        deck_after = (await client.get(f"/api/decks/{deck['id']}")).json()
        assert not any(c["name"] == "Lightning Bolt" for c in deck_after["cards"])

    async def test_undo_returns_409_if_state_diverged(self, client, deck):
        # Movemos a sideboard, luego movemos otra vez a maybeboard,
        # después intentamos deshacer el PRIMER movimiento → 409
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "sideboard"})
        # Recuperamos ID del primer evento antes del segundo cambio
        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        first_move = next(e for e in events if e["kind"] == "card_moved")

        # Segundo movimiento
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "maybeboard"})

        # Intentar deshacer el primero → 409 (ya no está en sideboard)
        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{first_move['id']}/undo"
        )
        assert r.status_code == 409

    async def test_undo_non_reversible_returns_400(self, client, deck):
        # deck_created no es reversible
        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        created = next(e for e in events if e["kind"] == "deck_created")

        r = await client.post(
            f"/api/decks/{deck['id']}/activity/{created['id']}/undo"
        )
        assert r.status_code == 400

    async def test_undo_emits_new_event_in_timeline(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "sideboard"})
        events_before = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        moved = next(e for e in events_before if e["kind"] == "card_moved")
        await client.post(
            f"/api/decks/{deck['id']}/activity/{moved['id']}/undo"
        )

        events_after = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        assert len(events_after) == len(events_before) + 1
        # El evento nuevo tiene undone_event_id en su payload
        newest = events_after[0]
        assert newest["payload"].get("undone_event_id") == moved["id"]


# =============================================================================
# Progreso de build
# =============================================================================

class TestBuildProgress:
    async def test_progress_endpoint_returns_inactive_by_default(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/build-progress")
        assert r.status_code == 200
        data = r.json()
        assert data["active"] is False
        assert data["deck_id"] == deck["id"]

    async def test_progress_state_after_manual_start(self, client, deck):
        """Simulamos que el servicio se ha iniciado — el endpoint debe reflejarlo.

        No podemos ejecutar build_xml aquí (necesitaría descargar arte real),
        pero sí podemos verificar que el módulo build_progress se lee bien.
        """
        from mpc_forge.services import build_progress
        build_progress.start(deck["id"], total=10, kind="xml")
        build_progress.tick(deck["id"], "Sol Ring")
        build_progress.tick(deck["id"], "Command Tower")

        r = await client.get(f"/api/decks/{deck['id']}/build-progress")
        data = r.json()
        assert data["active"] is True
        assert data["total"] == 10
        assert data["current"] == 2
        assert data["current_name"] == "Command Tower"
        assert data["done"] is False
        assert data["percent"] == 20.0

        build_progress.finish(deck["id"])
        r = await client.get(f"/api/decks/{deck['id']}/build-progress")
        assert r.json()["done"] is True

        build_progress.clear(deck["id"])


# =============================================================================
# Performance: endpoint de validation ligero + caching HTTP + list_decks ligero
# =============================================================================

class TestPerformanceEndpoints:
    async def test_validation_endpoint_returns_only_validation(self, client, deck):
        """Nuevo endpoint /validation: alternativa ligera a GET /{id} para
        cuando solo hace falta refrescar contadores tras un toggle."""
        r = await client.get(f"/api/decks/{deck['id']}/validation")
        assert r.status_code == 200
        val = r.json()
        # DeckValidation tiene estos campos, y solo estos
        assert "format" in val
        assert "expected" in val
        assert "counted" in val
        assert "is_valid" in val
        assert "message" in val
        assert "level" in val
        assert "breakdown" in val
        # NO debe incluir las cartas (esa es la mejora — se ahorra ~50-80KB
        # de payload para un mazo commander)
        assert "cards" not in val

    async def test_validation_endpoint_404_for_missing_deck(self, client):
        r = await client.get("/api/decks/99999/validation")
        assert r.status_code == 404

    async def test_list_decks_returns_summary_view(self, client, deck):
        """list_decks devuelve DeckSummaryView (ligero), no DeckView completo."""
        r = await client.get("/api/decks/")
        assert r.status_code == 200
        summaries = r.json()
        s = next(d for d in summaries if d["id"] == deck["id"])
        # Contiene lo esencial
        assert "card_count" in s
        assert s["card_count"] == 3
        # NO trae las cartas (esa es la optimización)
        assert "cards" not in s
        # NO trae validation (era muy pesada para un listing)
        assert "validation" not in s

    async def test_static_endpoints_have_cache_control(self, client):
        r = await client.get("/api/decks/_/supported-langs")
        assert r.status_code == 200
        assert "cache-control" in {k.lower() for k in r.headers}
        assert "max-age" in r.headers["cache-control"].lower()

        r = await client.get("/api/decks/_/undoable-kinds")
        assert "cache-control" in {k.lower() for k in r.headers}
