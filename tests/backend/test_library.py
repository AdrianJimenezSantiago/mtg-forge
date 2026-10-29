from __future__ import annotations

import html
import json
import re

from mpc_forge.services.decks import library


class TestTags:
    def test_tags_are_trimmed_deduplicated_and_capped(self):
        raw = ["  Cube ", "cube", "", "Pending  print", "x" * 50, *[f"t{i}" for i in range(20)]]
        tags = library.normalize_tags(raw)
        assert tags[:3] == ["Cube", "Pending print", "x" * library.MAX_TAG_LENGTH]
        assert len(tags) == library.MAX_TAGS

    def test_stored_tags_survive_bad_data(self):
        assert library.parse_tags("") == []
        assert library.parse_tags("not json") == []
        assert library.parse_tags('{"a": 1}') == []
        assert library.parse_tags(library.serialize_tags(["Á", "b"])) == ["Á", "b"]

    def test_colors_follow_the_wubrg_order(self):
        assert library.sort_colors("GWr") == "WRG"
        assert library.sort_colors("") == ""


class TestDeckTagsApi:
    async def test_tags_are_saved_listed_and_copied(self, client, deck):
        r = await client.patch(f"/api/decks/{deck['id']}", json={"tags": ["Cube", " cube ", "EDH"]})
        assert r.status_code == 200 and r.json()["tags"] == ["Cube", "EDH"]
        listed = (await client.get("/api/decks/")).json()
        assert listed[0]["tags"] == ["Cube", "EDH"]

        dup = await client.post(f"/api/decks/{deck['id']}/duplicate", json={"name": "Copy"})
        assert dup.status_code == 201 and dup.json()["tags"] == ["Cube", "EDH"]
        await client.patch(f"/api/decks/{dup.json()['id']}", json={"tags": ["EDH"]})

        counts = (await client.get("/api/decks/_/tags")).json()
        assert counts == [{"tag": "EDH", "count": 2}, {"tag": "Cube", "count": 1}]

    async def test_notes_and_name_are_untouched_by_a_tags_update(self, client, deck):
        await client.patch(f"/api/decks/{deck['id']}", json={"notes": "keep"})
        r = await client.patch(f"/api/decks/{deck['id']}", json={"tags": []})
        assert r.json()["notes"] == "keep" and r.json()["name"] == "Test Deck"
        assert r.json()["tags"] == []


class TestStats:
    async def test_stats_cover_size_colors_and_custom_art(self, client):
        r = await client.post(
            "/api/decks/import/text",
            json={"name": "Red", "text": "1 Lightning Bolt\n2 Sol Ring", "format": "modern"},
        )
        deck_id = r.json()["deck"]["id"]
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck

        async with session_scope() as db:
            deck = await db.get(Deck, deck_id)
            stats = (await library.stats_for_decks(db, [deck]))[deck_id]
        assert stats.colors == "R" and stats.total == 3
        assert (stats.counted, stats.expected, stats.level) == (3, 60, "warn")
        assert stats.custom_art == 0 and stats.excluded == 0

        cards = (await client.get(f"/api/decks/{deck_id}")).json()["cards"]
        bolt = next(c for c in cards if c["name"] == "Lightning Bolt")
        await client.post(f"/api/decks/{deck_id}/cards/{bolt['id']}/toggle")
        async with session_scope() as db:
            deck = await db.get(Deck, deck_id)
            stats = (await library.stats_for_decks(db, [deck]))[deck_id]
        assert stats.total == 2 and stats.excluded == 1 and stats.colors == ""

    async def test_no_decks_means_no_stats(self, client):
        from mpc_forge.db import session_scope

        async with session_scope() as db:
            assert await library.stats_for_decks(db, []) == {}


class TestLibraryPage:
    async def test_page_ships_the_filter_metadata(self, client, deck):
        await client.patch(f"/api/decks/{deck['id']}", json={"tags": ["Cube"], "notes": "n"})
        r = await client.get("/decks")
        assert r.status_code == 200
        page = r.text
        assert 'data-testid="library-toolbar"' in page and "deckLibrary()" in page
        raw = re.search(r"data-decks='([^']*)'", page).group(1)
        meta = json.loads(html.unescape(raw))
        assert meta[0]["name"] == "Test Deck" and meta[0]["tags"] == ["Cube"]
        assert meta[0]["cards"] == 3 and meta[0]["format"] == "commander"
        assert "#Cube" in page and "3/100" in page

    async def test_names_with_quotes_stay_inside_the_attribute(self, client):
        await client.post(
            "/api/decks/import/text",
            json={"name": "Urza's <Saga>", "text": "1 Sol Ring", "format": "commander"},
        )
        page = (await client.get("/decks")).text
        raw = re.search(r"data-decks='([^']*)'", page).group(1)
        assert json.loads(html.unescape(raw))[0]["name"] == "Urza's <Saga>"
