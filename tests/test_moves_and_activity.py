"""Tests de mover/vaciar secciones y del timeline de actividad."""
from __future__ import annotations


class TestMoveBetweenSections:
    """Feature: mover cartas entre secciones y vaciar sección entera."""

    async def test_move_card_to_sideboard(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        r = await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                               json={"role": "sideboard"})
        assert r.status_code == 200
        assert r.json()["role"] == "sideboard"

    async def test_clear_role_removes_all_cards(self, client, deck):
        # Movemos 2 cartas a sideboard
        for name in ["Sol Ring", "Command Tower"]:
            c = next(c for c in deck["cards"] if c["name"] == name)
            await client.patch(f"/api/decks/{deck['id']}/cards/{c['id']}",
                               json={"role": "sideboard"})

        r = await client.delete(f"/api/decks/{deck['id']}/role/sideboard")
        assert r.status_code == 200
        assert r.json() == {"role": "sideboard", "deleted": 2}

    async def test_clear_empty_role_returns_zero_deleted(self, client, deck):
        r = await client.delete(f"/api/decks/{deck['id']}/role/tokens")
        assert r.status_code == 200
        assert r.json()["deleted"] == 0

    async def test_clear_role_on_nonexistent_deck_returns_404(self, client):
        r = await client.delete("/api/decks/99999/role/sideboard")
        assert r.status_code == 404


class TestActivityTimeline:
    """Feature: timeline de actividad por mazo."""

    async def test_activity_endpoint_returns_events(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/activity")
        assert r.status_code == 200
        events = r.json()
        # Al crear el mazo se emite deck_created
        assert len(events) >= 1
        assert events[-1]["kind"] == "deck_created"  # el más antiguo

    async def test_activity_ordered_desc_by_date(self, client, deck):
        # Encadenamos operaciones
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "sideboard"})
        await client.patch(f"/api/decks/{deck['id']}", json={"name": "Renamed"})

        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        kinds = [e["kind"] for e in events]
        # El más reciente primero, el más antiguo (creation) al final
        assert kinds[0] == "deck_renamed"
        assert kinds[1] == "card_moved"
        assert kinds[-1] == "deck_created"

    async def test_activity_filter_by_kinds(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Sol Ring")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"role": "sideboard"})
        await client.patch(f"/api/decks/{deck['id']}", json={"name": "Renamed"})

        r = await client.get(
            f"/api/decks/{deck['id']}/activity?kinds=card_moved,card_added"
        )
        events = r.json()
        assert all(e["kind"] in {"card_moved", "card_added"} for e in events)

    async def test_activity_snapshots_previous_state(self, client, deck):
        card = next(c for c in deck["cards"] if c["name"] == "Command Tower")
        await client.patch(f"/api/decks/{deck['id']}/cards/{card['id']}",
                           json={"quantity": 3})
        events = (await client.get(f"/api/decks/{deck['id']}/activity")).json()
        qty_event = next(e for e in events if e["kind"] == "card_qty_changed")
        assert qty_event["payload"]["old_qty"] == 1
        assert qty_event["payload"]["new_qty"] == 3

    async def test_activity_on_nonexistent_deck_returns_404(self, client):
        r = await client.get("/api/decks/99999/activity")
        assert r.status_code == 404

    async def test_with_activity_endpoint_returns_deck_summaries(self, client, deck):
        r = await client.get("/api/decks/_/with-activity")
        assert r.status_code == 200
        summaries = r.json()
        assert len(summaries) >= 1
        s = next(d for d in summaries if d["id"] == deck["id"])
        assert s["activity_count"] >= 1
        assert s["last_activity_kind"] == "deck_created"
