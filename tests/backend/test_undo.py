from __future__ import annotations

import json

import pytest

from mpc_forge.models import DeckActivity
from mpc_forge.services.decks import undo


async def _cards(client, deck_id: int) -> dict[str, dict]:
    return {c["name"]: c for c in (await client.get(f"/api/decks/{deck_id}")).json()["cards"]}


async def _last(client, deck_id: int, kind: str) -> dict:
    events = (await client.get(f"/api/decks/{deck_id}/activity")).json()
    return next(e for e in events if e["kind"] == kind)


async def _undo(client, deck_id: int, event_id: int):
    return await client.post(f"/api/decks/{deck_id}/activity/{event_id}/undo")


class TestUndoRoundTrips:
    async def test_quantity_and_conflicts(self, client, deck):
        d = deck["id"]
        sol = (await _cards(client, d))["Sol Ring"]
        await client.patch(f"/api/decks/{d}/cards/{sol['id']}", json={"quantity": 3})
        event = await _last(client, d, "card_qty_changed")
        await client.patch(f"/api/decks/{d}/cards/{sol['id']}", json={"quantity": 4})
        r = await _undo(client, d, event["id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "undo_quantity_changed"
        assert r.json()["detail"]["params"] == {"expected": 3, "current": 4}

        latest = await _last(client, d, "card_qty_changed")
        assert (await _undo(client, d, latest["id"])).status_code == 200
        assert (await _cards(client, d))["Sol Ring"]["quantity"] == 3

    async def test_include_toggle(self, client, deck):
        d = deck["id"]
        sol = (await _cards(client, d))["Sol Ring"]
        await client.post(f"/api/decks/{d}/cards/{sol['id']}/toggle")
        event = await _last(client, d, "card_include_toggled")
        assert (await _undo(client, d, event["id"])).status_code == 200
        assert (await _cards(client, d))["Sol Ring"]["include"] is True

    async def test_official_art_change(self, client, deck):
        d = deck["id"]
        sol = (await _cards(client, d))["Sol Ring"]
        r = await client.post(
            f"/api/decks/{d}/cards/change-art",
            json={"deck_card_id": sol["id"], "scryfall_id": "sr-alt"},
        )
        assert r.status_code == 200
        event = await _last(client, d, "card_art_changed")
        r = await _undo(client, d, event["id"])
        assert r.status_code == 200, r.text
        assert (await _cards(client, d))["Sol Ring"]["scryfall_id"] == "sr-en"
        again = await _undo(client, d, event["id"])
        assert again.status_code == 409 and again.json()["detail"]["code"] == "undo_art_changed"

    async def test_rename_and_rename_again(self, client, deck):
        d = deck["id"]
        await client.patch(f"/api/decks/{d}", json={"name": "Second"})
        first = await _last(client, d, "deck_renamed")
        await client.patch(f"/api/decks/{d}", json={"name": "Third"})
        r = await _undo(client, d, first["id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "undo_renamed_again"
        latest = await _last(client, d, "deck_renamed")
        assert (await _undo(client, d, latest["id"])).status_code == 200
        assert (await client.get(f"/api/decks/{d}")).json()["name"] == "Second"

    async def test_added_card_is_removed_again(self, client, deck):
        d = deck["id"]
        r = await client.post(f"/api/decks/{d}/cards", json={"name": "Lightning Bolt"})
        assert r.status_code == 201, r.text
        event = await _last(client, d, "card_added")
        assert (await _undo(client, d, event["id"])).status_code == 200
        assert "Lightning Bolt" not in await _cards(client, d)

    async def test_removed_card_comes_back_once(self, client, deck):
        d = deck["id"]
        sol = (await _cards(client, d))["Sol Ring"]
        await client.delete(f"/api/decks/{d}/cards/{sol['id']}")
        event = await _last(client, d, "card_removed")
        assert (await _undo(client, d, event["id"])).status_code == 200
        assert (await _cards(client, d))["Sol Ring"]["quantity"] == 1
        dup = await _undo(client, d, event["id"])
        assert dup.status_code == 409 and dup.json()["detail"]["code"] == "undo_card_already_in"

    async def test_moved_card_that_moved_again(self, client, deck):
        d = deck["id"]
        sol = (await _cards(client, d))["Sol Ring"]
        await client.patch(f"/api/decks/{d}/cards/{sol['id']}", json={"role": "sideboard"})
        first = await _last(client, d, "card_moved")
        await client.patch(f"/api/decks/{d}/cards/{sol['id']}", json={"role": "maybeboard"})
        r = await _undo(client, d, first["id"])
        assert r.status_code == 409 and r.json()["detail"]["params"] == {"role": "sideboard"}

    async def test_events_that_cannot_be_undone(self, client, deck):
        d = deck["id"]
        created = await _last(client, d, "deck_created")
        r = await _undo(client, d, created["id"])
        assert r.status_code == 400 and r.json()["detail"]["code"] == "undo_kind_not_reversible"
        assert (await _undo(client, d, 999_999)).status_code == 404


def _event(kind: str, payload, deck_id: int | None = 1) -> DeckActivity:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return DeckActivity(id=1, deck_id=deck_id, kind=kind, payload_json=raw, card_name="X")


class TestCanUndo:
    @pytest.mark.parametrize(
        ("kind", "payload", "code"),
        [
            ("pdf_generated", {}, "undo_kind_not_reversible"),
            ("card_moved", "{not json", "undo_payload_corrupt"),
            ("card_art_changed", {"kind": "custom"}, "undo_custom_art"),
            ("card_art_changed", {"kind": "official"}, "undo_missing_previous_art"),
            ("custom_art_applied", {"changes": []}, "undo_no_changes_saved"),
        ],
    )
    async def test_reasons(self, kind, payload, code):
        assert await undo.can_undo(_event(kind, payload)) == (False, code)

    async def test_ok(self):
        assert await undo.can_undo(_event("card_moved", {"from_role": "a"})) == (True, "")

    async def test_events_without_a_live_deck(self, client):
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            with pytest.raises(undo.UndoNotSupported) as exc:
                await undo.undo_event(db, _event("card_moved", {}, deck_id=None))
            assert exc.value.code == "undo_no_deck"
            with pytest.raises(undo.UndoNotSupported) as exc:
                await undo.undo_event(db, _event("card_moved", {}, deck_id=4242))
            assert exc.value.code == "undo_deck_gone"

    async def test_incomplete_snapshots(self, client, deck):
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            for kind, payload in [
                ("card_moved", {}),
                ("card_qty_changed", {}),
                ("card_art_changed", {"old_scryfall_id": "x"}),
                ("deck_renamed", {}),
                ("card_removed", {}),
            ]:
                with pytest.raises(undo.UndoNotSupported) as exc:
                    await undo.undo_event(db, _event(kind, payload, deck_id=deck["id"]))
                assert exc.value.code in {"undo_incomplete_snapshot", "undo_card_gone"}, kind
