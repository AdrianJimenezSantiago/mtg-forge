from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import update

from mpc_forge.clients.scryfall import HEAVY_PATHS, ScryfallClient
from mpc_forge.db import session_scope
from mpc_forge.models import DFCPair, PrintingCache
from mpc_forge.services.cards import bulk_data, preloader, printings
from mpc_forge.services.decks import importer
from mpc_forge.utils import rate_limiter
from mpc_forge.utils.rate_limiter import TieredRateLimiter

_SCALE = 5 if sys.platform == "win32" else 2
HEAVY = 0.10 * _SCALE
GENERAL = 0.02 * _SCALE
COOLDOWN = 0.3 * _SCALE
JITTER = 0.06 if sys.platform == "win32" else 0.008

DECK_TEXT = "1 Sol Ring\n1 Command Tower\n1 Arcane Signet"


class FakeScryfall:
    def __init__(self, *, force_429: int = 0) -> None:
        self.force_429 = force_429
        self.last_heavy = self.last_general = -1e9
        self.blocked_until = 0.0
        self.calls: list[str] = []
        self.violations = 0
        self.rate_limited = 0

    def _limited(self) -> httpx.Response:
        self.rate_limited += 1
        return httpx.Response(429, json={"object": "error", "status": 429})

    def handler(self, request: httpx.Request) -> httpx.Response:
        now = time.monotonic()
        path = request.url.path
        self.calls.append(path)
        if now < self.blocked_until:
            return self._limited()
        if self.force_429 > 0:
            self.force_429 -= 1
            self.blocked_until = now + COOLDOWN
            return self._limited()
        if path in HEAVY_PATHS:
            if now - self.last_heavy < HEAVY - JITTER:
                self.violations += 1
                self.blocked_until = now + COOLDOWN
                return self._limited()
            self.last_heavy = now
        if now - self.last_general < GENERAL - JITTER:
            self.violations += 1
            self.blocked_until = now + COOLDOWN
            return self._limited()
        self.last_general = now
        return self._respond(request)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cards/collection":
            idents = json.loads(request.content)["identifiers"]
            data = [
                {"id": f"id-{i.get('name') or i.get('id')}", "name": i.get("name", "x")}
                for i in idents
            ]
            return httpx.Response(200, json={"data": data})
        if path == "/cards/search":
            page = int(request.url.params.get("page", "1"))
            next_page = (
                f"https://api.scryfall.com/cards/search?q=x&page={page + 1}" if page < 3 else None
            )
            return httpx.Response(
                200,
                json={"data": [{"id": f"p{page}"}], "has_more": page < 3, "next_page": next_page},
            )
        if path == "/cards/autocomplete":
            return httpx.Response(200, json={"data": ["Sol Ring"]})
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})


def make_client(server: FakeScryfall) -> ScryfallClient:
    http = httpx.AsyncClient(
        base_url="https://api.scryfall.com", transport=httpx.MockTransport(server.handler)
    )
    return ScryfallClient(
        http, general_interval=GENERAL * 1.1, heavy_interval=HEAVY * 1.1, cooldown=COOLDOWN
    )


class TestScryfallClient:
    async def test_mixed_concurrent_burst_never_trips_the_limit(self):
        server = FakeScryfall()
        sc = make_client(server)

        async def one_import(n: int) -> None:
            await sc.collection([{"name": f"card-{n}-{i}"} for i in range(150)])
            await asyncio.gather(*(sc.by_id(f"{n}-{i}") for i in range(5)))
            await sc.named(f"Commander {n}")

        await asyncio.gather(*(one_import(n) for n in range(5)))
        assert (server.violations, server.rate_limited) == (0, 0)
        await sc.aclose()

    async def test_heavy_and_light_lanes(self):
        server = FakeScryfall()
        sc = make_client(server)
        start = time.monotonic()
        await asyncio.gather(*(sc.named(f"card {i}") for i in range(5)))
        assert time.monotonic() - start >= 4 * HEAVY and server.rate_limited == 0
        start = time.monotonic()
        await asyncio.gather(*(sc.by_id(f"id-{i}") for i in range(5)))
        assert time.monotonic() - start < 4 * HEAVY
        assert [c["id"] for c in await sc.search_all("x")] == ["p1", "p2", "p3"]
        assert server.violations == 0
        await sc.aclose()

    async def test_429_triggers_a_shared_cooldown_and_a_single_retry(self):
        server = FakeScryfall(force_429=1)
        sc = make_client(server)
        start = time.monotonic()
        results = await asyncio.gather(*(sc.by_id(f"id-{i}") for i in range(4)))
        assert all(r.get("id") for r in results) and time.monotonic() - start >= COOLDOWN
        assert server.rate_limited == sc.stats["rate_limited"] == 1
        await sc.aclose()

        server = FakeScryfall(force_429=2)
        sc = make_client(server)
        with pytest.raises(httpx.HTTPStatusError):
            await sc.by_id("abc")
        assert server.rate_limited == 2
        await sc.aclose()

        server = FakeScryfall()
        sc = make_client(server)
        sc._start_cooldown(10)
        start = time.monotonic()
        assert await sc.autocomplete("sol") == []
        assert time.monotonic() - start < 0.1 and server.calls == []
        await sc.aclose()

    async def test_identical_requests_are_deduplicated(self):
        server = FakeScryfall()
        sc = make_client(server)
        results = await asyncio.gather(*(sc.by_id("same") for _ in range(6)))
        assert all(r == {"id": "same"} for r in results)
        assert server.calls.count("/cards/same") == 1 and sc.stats["deduplicated"] == 5

        out = await sc.collection([{"name": "Sol Ring"}] * 80 + [{"name": "Arcane Signet"}])
        assert server.calls.count("/cards/collection") == 1 and len(out) == 2

        t1, t2 = asyncio.create_task(sc.search_all("x")), asyncio.create_task(sc.search_all("x"))
        await asyncio.sleep(0)
        t1.cancel()
        assert len(await t2) == 3
        with pytest.raises(asyncio.CancelledError):
            await t1
        await sc.aclose()

    async def test_prints_for_many_oracles_use_one_search(self):
        server = FakeScryfall()
        sc = make_client(server)
        seen: list[str] = []
        original = server.handler

        def spy(request):
            if request.url.path == "/cards/search" and "page" not in request.url.params:
                seen.append(request.url.params["q"])
            return original(request)

        sc._client._transport = httpx.MockTransport(spy)
        await sc.prints_by_oracle_ids(["a", "b", "a", "c"])
        assert seen == ["(oracleid:a or oracleid:b or oracleid:c) include:extras"]
        await sc.aclose()

    def test_tiered_limiter_reservations(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: clock["now"])

        lim = TieredRateLimiter(general=0.1, heavy=0.5)
        slots = [
            (h, lim.reserve(h))
            for h in (True, False, False, False, False, False, True, False, True)
        ]
        heavy = sorted(t for h, t in slots if h)
        every = sorted(t for _, t in slots)
        assert all(b - a >= 0.5 - 1e-9 for a, b in zip(heavy, heavy[1:]))
        assert all(b - a >= 0.1 - 1e-9 for a, b in zip(every, every[1:]))

        lim = TieredRateLimiter(general=0.1, heavy=0.5)
        assert lim.reserve(True) == 1000.0
        assert lim.reserve(True) == pytest.approx(1000.5)
        assert lim.reserve(False) == pytest.approx(1000.1)


class _CountingScryfall:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.collection_calls: list[list[dict]] = []
        self.prints_calls: list[str] = []

    async def collection(self, idents):
        self.collection_calls.append(list(idents))
        return await self.inner.collection(idents)

    async def prints_by_oracle_id(self, oid):
        self.prints_calls.append(oid)
        return []

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture
def counting(client, fake_scryfall):
    printings._name_memo.clear()
    printings._prints_complete.clear()
    return _CountingScryfall(fake_scryfall)


async def _import(scryfall, text: str = DECK_TEXT):
    async with session_scope() as db:
        deck, unresolved = await importer.import_from_plaintext(db, scryfall, "Deck", text)
        return deck.id, unresolved


class TestLocalCardCache:
    async def test_repeated_imports_only_query_unknown_cards(self, counting):
        await _import(counting)
        await _import(counting)
        await _import(counting)
        assert len(counting.collection_calls) == 1
        await _import(counting, DECK_TEXT + "\n1 Lightning Bolt")
        assert counting.collection_calls[-1] == [{"name": "Lightning Bolt"}]

        entries = [{"name": "Sol Ring", "quantity": 1, "scryfall_id": "sr-en", "role": "mainboard"}]
        async with session_scope() as db:
            [resolved] = await printings.resolve_cards(db, counting, entries)
        assert resolved["resolved"] and resolved["scryfall_id"] == "sr-en"
        assert len(counting.collection_calls) == 2

    async def test_stale_rows_are_refreshed(self, counting):
        await _import(counting)
        async with session_scope() as db:
            await db.execute(
                update(PrintingCache).values(fetched_at=datetime.now(UTC) - timedelta(days=30))
            )
            await db.commit()
        await _import(counting)
        assert len(counting.collection_calls) == 2

    async def test_front_face_name_resolves_a_dfc(self, counting):
        dfc = {
            "id": "dos-en",
            "oracle_id": "oracle-delver",
            "set": "isd",
            "collector_number": "51",
            "lang": "en",
            "layout": "transform",
            "name": "Delver of Secrets // Insectile Aberration",
            "card_faces": [
                {"name": "Delver of Secrets", "image_uris": {"png": "https://x.test/f.png"}},
                {"name": "Insectile Aberration", "image_uris": {"png": "https://x.test/b.png"}},
            ],
        }

        async def collection(idents):
            return [dfc] if any(i.get("name") == "Delver of Secrets" for i in idents) else []

        counting.inner.collection = collection
        assert (await _import(counting, "1 Delver of Secrets"))[1] == []

    async def test_printings_are_fetched_once_and_preloaded_in_batches(
        self, counting, deck, monkeypatch
    ):
        await _import(counting)
        for _ in range(3):
            async with session_scope() as db:
                await printings.fetch_printings_for_oracle(db, counting, "oracle-command-tower")
        assert counting.prints_calls == ["oracle-command-tower"]

        printings._prints_complete.clear()
        batches: list[list[str]] = []

        async def prints_by_oracle_ids(oids):
            batches.append(list(oids))
            return []

        counting.prints_by_oracle_ids = prints_by_oracle_ids
        monkeypatch.setattr(printings, "PRINTS_BATCH_SIZE", 2)
        state = await preloader.start(deck["id"], counting)
        await state.task
        assert sorted(len(b) for b in batches) == [1, 2] and state.done == state.total == 3

        batches.clear()
        state = await preloader.start(deck["id"], counting)
        await state.task
        assert batches == [] and state.done == state.total == 3


SIMPLE_CARD = {
    "id": "aaaa-1111",
    "oracle_id": "oracle-sol-ring",
    "name": "Sol Ring",
    "set": "LEA",
    "set_name": "Limited Edition Alpha",
    "collector_number": "270",
    "rarity": "uncommon",
    "lang": "en",
    "layout": "normal",
    "mana_cost": "{1}",
    "cmc": 1.0,
    "type_line": "Artifact",
    "artist": "Mark Tedin",
    "released_at": "1993-08-05",
    "image_uris": {
        "normal": "https://img/normal.jpg",
        "large": "https://img/large.jpg",
        "png": "https://img/card.png",
    },
}
DFC_CARD = {
    "id": "bbbb-2222",
    "oracle_id": "oracle-delver",
    "name": "Delver of Secrets // Insectile Aberration",
    "set": "ISD",
    "collector_number": "51",
    "layout": "transform",
    "cmc": 1.0,
    "card_faces": [
        {
            "name": "Delver of Secrets",
            "mana_cost": "{U}",
            "type_line": "Creature — Human Wizard",
            "colors": ["U"],
            "image_uris": {"normal": "https://img/front.jpg"},
        },
        {
            "name": "Insectile Aberration",
            "type_line": "Creature",
            "image_uris": {"normal": "https://img/back.jpg"},
        },
    ],
}
TOKEN_MAKER = {
    "id": "cccc-3333",
    "oracle_id": "oracle-krenko",
    "name": "Krenko, Mob Boss",
    "layout": "normal",
    "all_parts": [
        {"id": "tok-1", "name": "Goblin", "component": "token"},
        {"id": "self", "name": "Krenko, Mob Boss", "component": "combo_piece"},
    ],
}


class TestBulkData:
    def test_cards_are_mapped_to_printing_rows(self):
        row = bulk_data.card_to_row(SIMPLE_CARD)
        assert (row["scryfall_id"], row["oracle_id"], row["name"], row["set_code"]) == (
            "aaaa-1111",
            "oracle-sol-ring",
            "Sol Ring",
            "lea",
        )
        assert (row["image_png"], row["artist"], row["related_parts"]) == (
            "https://img/card.png",
            "Mark Tedin",
            "",
        )

        dfc = bulk_data.card_to_row(DFC_CARD)
        assert (dfc["mana_cost"], dfc["colors"]) == ("{U}", "U") and dfc["type_line"].startswith(
            "Creature"
        )
        assert (dfc["back_name"], dfc["back_image_normal"]) == (
            "Insectile Aberration",
            "https://img/back.jpg",
        )

        parts = json.loads(bulk_data.card_to_row(TOKEN_MAKER)["related_parts"])
        assert {p["component"] for p in parts} == {"token"}

        assert bulk_data.card_to_row({"id": "d", "layout": "art_series", "name": "x"}) is None
        assert bulk_data.card_to_row({"name": "Rota"}) is None
        minimal = bulk_data.card_to_row(
            {"id": "x", "oracle_id": "y", "name": "Z", "layout": "normal"}
        )
        assert (minimal["cmc"], minimal["colors"], minimal["image_normal"]) == (0.0, "", None)

    def test_incremental_parser_is_independent_of_chunk_boundaries(self):
        def parse(text: str, chunk: int) -> list[dict]:
            parser = bulk_data._IncrementalArrayParser()
            return [
                card
                for i in range(0, len(text), chunk)
                for card in parser.feed(text[i : i + chunk])
            ]

        payload = json.dumps([SIMPLE_CARD, DFC_CARD, TOKEN_MAKER])
        for chunk in (1, 3, 17, 128, 100000):
            assert [c["id"] for c in parse(payload, chunk)] == [
                "aaaa-1111",
                "bbbb-2222",
                "cccc-3333",
            ], chunk
        tricky = [
            {"id": "1", "name": "Counterspell", "mana_cost": "{U}{U}"},
            {"id": "2", "name": "Ach! Hans, Run!", "flavor": 'dijo \\"corre\\"'},
        ]
        parsed = parse(json.dumps(tricky), 5)
        assert len(parsed) == 2 and parsed[0]["mana_cost"] == "{U}{U}"
        assert parse("[]", 1) == []

        parser = bulk_data._IncrementalArrayParser()
        big = json.dumps([SIMPLE_CARD] * 50)
        consumed = sum(len(list(parser.feed(big[i : i + 512]))) for i in range(0, len(big), 512))
        assert consumed == 50 and len(parser._buf) < 4096

    def test_progress_reporting(self):
        assert bulk_data.BulkProgress().to_dict()["percent"] == 0.0
        assert (
            bulk_data.BulkProgress(bytes_total=1000, bytes_downloaded=250).to_dict()["percent"]
            == 25.0
        )
        active = {
            phase: bulk_data.BulkProgress(phase=phase).to_dict()["active"]
            for phase in ("idle", "manifest", "downloading", "importing", "done", "error")
        }
        assert active == {
            "idle": False,
            "manifest": True,
            "downloading": True,
            "importing": True,
            "done": False,
            "error": False,
        }

    async def test_endpoints(self, client):
        status = (await client.get("/api/bulk/status")).json()
        assert {"printings", "unique_cards", "progress"} <= set(status)
        progress = (await client.get("/api/bulk/progress")).json()
        assert (progress["phase"], progress["active"]) == ("idle", False)
        assert (await client.post("/api/bulk/sync?kind=inventado")).status_code == 400
        assert (await client.get("/api/bulk/check?kind=inventado")).status_code == 400
        cancel = await client.post("/api/bulk/cancel")
        assert cancel.status_code == 200 and cancel.json()["cancelled"] is False


async def test_dfc_pair_lookup(client):
    async with session_scope() as s:
        s.add(
            DFCPair(
                front_name="Delver of Secrets", back_name="Insectile Aberration", kind="transform"
            )
        )
        s.add(
            DFCPair(front_name="Bruna, the Fading Light", back_name="Brisela Top", kind="meld_top")
        )

    data = (
        await client.get(
            "/api/dfc-pairs/lookup?names=Delver of Secrets|Nonexistent|Bruna, the Fading Light"
        )
    ).json()
    assert (data["total_queried"], data["total_matched"]) == (3, 2)
    assert data["found"]["Delver of Secrets"]["back_name"] == "Insectile Aberration"
    assert data["found"]["Bruna, the Fading Light"]["kind"] == "meld_top"
    assert "Nonexistent" not in data["found"]
    assert (
        "DELVER of secrets"
        in (await client.get("/api/dfc-pairs/lookup?names=DELVER of secrets")).json()["found"]
    )
    assert (await client.get("/api/dfc-pairs/lookup?names=")).json()["total_queried"] == 0
