from __future__ import annotations

import asyncio
import gzip
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
from mpc_forge.services.art import recommender
from mpc_forge.services.art.art_cache import ArtCache
from mpc_forge.services.cards import bulk_data, images, preloader, printings
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


class TestImages:
    def test_legacy_jpeg_urls_map_to_their_webp_versions(self):
        base = "https://cards.scryfall.io/{}/back/6/9/69.{}?17"
        for old, new in images.WEBP_FOR.items():
            assert images.webp_url(base.format(old, "jpg")) == base.format(new, "webp")
        no_query = "https://cards.scryfall.io/large/front/6/9/69.jpg"
        assert images.webp_url(no_query) == "https://cards.scryfall.io/display/front/6/9/69.webp"
        for untouched in (
            None,
            "",
            "https://cards.scryfall.io/png/front/6/9/69.png?17",
            "https://cards.scryfall.io/grid/front/6/9/69.webp?17",
            "/static/img/logo.png",
        ):
            assert images.webp_url(untouched) == untouched

    def test_printings_store_webp_and_keep_png_for_printing(self):
        new_api = {
            "grid": "G",
            "display": "D",
            "thumb": "T",
            "jpeg": "https://cards.scryfall.io/normal/front/a/b/x.jpg",
            "png": "P",
        }
        row = printings.printing_fields({**SIMPLE_CARD, "image_uris": new_api})
        assert (row["image_normal"], row["image_large"], row["image_png"]) == ("G", "D", "P")
        legacy = printings.printing_fields(
            {
                **SIMPLE_CARD,
                "image_uris": {
                    "normal": "https://cards.scryfall.io/normal/front/a/b/x.jpg?1",
                    "large": "https://cards.scryfall.io/large/front/a/b/x.jpg?1",
                    "png": "https://cards.scryfall.io/png/front/a/b/x.png?1",
                },
            }
        )
        assert legacy["image_normal"] == "https://cards.scryfall.io/grid/front/a/b/x.webp?1"
        assert legacy["image_large"] == "https://cards.scryfall.io/display/front/a/b/x.webp?1"
        assert legacy["image_png"] == "https://cards.scryfall.io/png/front/a/b/x.png?1"
        only_jpeg = {"jpeg": "https://cards.scryfall.io/normal/front/a/b/x.jpg"}
        assert images.grid(only_jpeg) == "https://cards.scryfall.io/grid/front/a/b/x.webp"
        assert (images.display({}), images.thumb({"small": None})) == (None, None)


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
        assert {p["component"] for p in parts} == {"token", "combo_piece"}

        priced = {**SIMPLE_CARD, "digital": True, "prices": {"usd": "1.50", "eur": None}}
        row = bulk_data.card_to_row(priced)
        expected = printings.printing_fields(priced)
        assert {k: v for k, v in row.items() if k not in ("scryfall_id", "fetched_at")} == {
            k: v for k, v in expected.items() if k != "fetched_at"
        }
        assert (row["price_usd"], row["price_eur"], row["digital"]) == (1.5, None, True)

        dfc = printings.printing_fields(
            {
                **DFC_CARD,
                "set_type": "expansion",
                "frame_effects": ["showcase", "extendedart"],
                "image_status": "highres_scan",
                "image_updated_at": "2026-07-13T02:02:53Z",
                "card_faces": [{**DFC_CARD["card_faces"][0], "illustration_id": "ill"}, {}],
            }
        )
        assert (dfc["set_type"], dfc["frame_effects"], dfc["illustration_id"]) == (
            "expansion",
            "showcase,extendedart",
            "ill",
        )
        assert (dfc["image_status"], dfc["image_updated_at"]) == (
            "highres_scan",
            "2026-07-13T02:02:53Z",
        )

        assert bulk_data.card_to_row({"id": "d", "layout": "art_series", "name": "x"}) is None
        assert bulk_data.card_to_row({"name": "Rota"}) is None
        minimal = bulk_data.card_to_row(
            {"id": "x", "oracle_id": "y", "name": "Z", "layout": "normal"}
        )
        assert (minimal["cmc"], minimal["colors"], minimal["image_normal"]) == (0.0, "", None)

    async def test_jsonl_gz_reader_is_independent_of_chunk_boundaries(self):
        cards = [SIMPLE_CARD, DFC_CARD, TOKEN_MAKER, {"id": "4", "flavor": 'dijo "corre"\n'}]
        body = "\n".join(json.dumps(c) for c in cards).encode()
        payload = gzip.compress(body)

        async def read(data: bytes, size: int) -> list[str]:
            async def chunks():
                for i in range(0, len(data), size):
                    yield data[i : i + size]

            return [c["id"] async for c in bulk_data._jsonl_cards(chunks())]

        expected = ["aaaa-1111", "bbbb-2222", "cccc-3333", "4"]
        for size in (1, 7, 300, 1 << 20):
            assert await read(payload, size) == expected, size
        # Un .gz puede traer varios miembros concatenados.
        two_members = gzip.compress(b"\n".join(json.dumps(c).encode() for c in cards[:2]) + b"\n")
        two_members += gzip.compress(json.dumps(cards[2]).encode())
        assert await read(two_members, 5) == expected[:3]
        assert await read(gzip.compress(b""), 4) == []

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


def _print(sfid: str, name: str, set_code: str, released: str, **extra) -> dict:
    return {
        "id": sfid,
        "oracle_id": f"oracle-{name.split(' // ')[0].lower()}",
        "name": name,
        "set": set_code,
        "collector_number": sfid,
        "lang": "en",
        "layout": "normal",
        "released_at": released,
        "prices": {"usd": "2.00"},
        "legalities": {"commander": "legal"},
        **extra,
    }


OFFLINE_PRINTS = [
    _print("bolt-old", "Lightning Bolt", "lea", "1993-08-05", artist="Christopher Rush"),
    _print(
        "bolt-new",
        "Lightning Bolt",
        "m11",
        "2010-07-16",
        artist="Christopher Moeller",
        frame_effects=["showcase"],
    ),
    _print("bolt-promo", "Lightning Bolt", "pm11", "2011-01-01", promo=True),
    _print("bolt-arena", "Lightning Bolt", "ha1", "2019-11-14", digital=True),
    _print("bolt-gold", "Lightning Bolt", "wc97", "2012-01-01", border_color="gold"),
    _print("bolt-fr", "Lightning Bolt", "fbb", "2015-01-01", lang="fr"),
    _print("bolt-preview", "Lightning Bolt", "fut", "2999-01-01"),
    _print("bolt-30a", "Lightning Bolt", "30a", "2022-11-28", set_type="memorabilia"),
    _print("bolt-blurry", "Lightning Bolt", "new", "2025-01-01", image_status="lowres"),
    _print("vial", "Aether Vial", "dst", "2004-02-06"),
    _print("eowyn", "Éowyn, Lady of Rohan", "ltr", "2023-06-23"),
    _print(
        "delver",
        "Delver of Secrets // Insectile Aberration",
        "isd",
        "2011-09-30",
        layout="transform",
    ),
    _print("goblin", "Goblin", "tm11", "2010-07-16", layout="token"),
]


async def _seed_offline(*, synced: bool = True) -> None:
    rows = [bulk_data.card_to_row(c) for c in OFFLINE_PRINTS]
    async with session_scope() as s:
        await bulk_data._flush(s, [r for r in rows if r])
        if synced:
            await bulk_data._record_state(s, "default_cards", "2026-10-01T00:00:00Z", len(rows), 0)


class TestOfflineLookups:
    async def test_bulk_upsert_keeps_every_column_and_uses_executemany(self, client):
        many = [
            bulk_data.card_to_row(_print(f"id-{i}", f"Card {i}", "tst", "2020-01-01"))
            for i in range(bulk_data.BATCH_SIZE)
        ]
        async with session_scope() as s:
            assert await bulk_data._flush(s, [r for r in many if r]) == bulk_data.BATCH_SIZE
            await bulk_data._flush(s, [bulk_data.card_to_row(_print("id-0", "Card 0", "tst", "x"))])
            row = await s.get(PrintingCache, "id-0")
            assert (row.price_usd, json.loads(row.legalities)) == (2.0, {"commander": "legal"})
            assert row.released_at == "x"

    async def test_names_resolve_locally_to_the_default_paper_printing(self, client, fake_scryfall):
        await _seed_offline()
        calls: list = []

        async def collection(idents):
            calls.append(idents)
            return []

        fake_scryfall.collection = collection
        printings._name_memo.clear()
        async with session_scope() as s:
            out = await printings.resolve_cards(
                s,
                fake_scryfall,
                [
                    {"name": "lightning bolt"},
                    {"name": "Lightning Bolt", "set": "LEA"},
                    {"name": "Delver of Secrets"},
                    {"name": "Æther Vial"},
                    {"name": "éowyn, lady of rohan"},
                    {"name": "Carta Inexistente"},
                ],
            )
        assert [o.get("scryfall_id") for o in out] == [
            "bolt-new",
            "bolt-old",
            "delver",
            "vial",
            "eowyn",
            None,
        ]
        assert calls == [[{"name": "Carta Inexistente"}]]

    async def test_names_are_not_guessed_from_a_partial_cache(self, client, fake_scryfall):
        await _seed_offline(synced=False)
        printings._name_memo.clear()
        async with session_scope() as s:
            out = await printings.resolve_cards(s, fake_scryfall, [{"name": "Lightning Bolt"}])
            assert await printings.find_local_printing(s, name="Lightning Bolt") is None
            assert await printings.local_autocomplete(s, "light") is None
        assert out[0]["scryfall_id"] == "lb-en"

    async def test_add_card_and_autocomplete_skip_the_api(self, client, deck, fake_scryfall):
        await _seed_offline()

        async def offline(*args, **kwargs):
            raise AssertionError("no debería llamar a Scryfall")

        fake_scryfall.named = fake_scryfall.by_set_and_number = offline
        fake_scryfall.autocomplete = offline

        r = await client.post(f"/api/decks/{deck['id']}/cards", json={"name": "Lightning Bolt"})
        assert r.status_code == 201, r.text
        assert r.json()["scryfall_id"] == "bolt-new"
        r = await client.post(
            f"/api/decks/{deck['id']}/cards",
            json={
                "name": "Lightning Bolt",
                "set_code": "LEA",
                "collector_number": "bolt-old",
                "role": "sideboard",
            },
        )
        assert r.json()["scryfall_id"] == "bolt-old"

        names = (await client.get("/api/decks/_/autocomplete?q=li")).json()
        assert names == ["Lightning Bolt"]
        assert (await client.get("/api/decks/_/autocomplete?q=SECRETS")).json() == [
            "Delver of Secrets // Insectile Aberration"
        ]
        assert (await client.get("/api/decks/_/autocomplete?q=gob")).json() == []
        assert (await client.get("/api/decks/_/autocomplete?q=_%25")).json() == []

    async def test_cancel_stops_a_running_import(self, client, monkeypatch):
        started = asyncio.Event()

        async def slow_sync(db, kind, *, force=False):
            started.set()
            bulk_data._progress = bulk_data.BulkProgress(kind=kind, phase="downloading")
            await asyncio.sleep(30)

        monkeypatch.setattr(bulk_data, "sync", slow_sync)
        task = bulk_data.start()
        await started.wait()
        with pytest.raises(RuntimeError):
            bulk_data.start()
        assert (await client.post("/api/bulk/sync")).status_code == 409
        assert (await client.post("/api/bulk/cancel")).json() == {"cancelled": True}
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not bulk_data.is_running()

    async def test_refresh_only_reimports_an_existing_stale_dump(self, client, monkeypatch):
        started: list[str] = []
        monkeypatch.setattr(bulk_data, "start", lambda kind="default_cards": started.append(kind))

        async def needs_sync(db, kind):
            return True, "nuevo"

        monkeypatch.setattr(bulk_data, "needs_sync", needs_sync)
        async with session_scope() as s:
            assert await bulk_data.refresh_if_stale(s) is False
            await _seed_offline()
            assert await bulk_data.refresh_if_stale(s) is False
            state = await bulk_data.get_state(s)
            state.synced_at = datetime.now(UTC) - bulk_data.REFRESH_AFTER - timedelta(hours=1)
            await s.commit()
            assert await bulk_data.refresh_if_stale(s) is True
        assert started == ["default_cards"]


class TestLocalRecommender:
    async def test_artist_and_style_come_from_the_dump(self, client, fake_scryfall):
        await _seed_offline()

        async def offline(*args, **kwargs):
            raise AssertionError("no debería llamar a Scryfall")

        fake_scryfall.prints_by_oracle_id = offline
        many = ["oracle-lightning bolt"] + [f"oracle-{i}" for i in range(40)]
        async with session_scope() as s:
            by_artist = await recommender.recommend_by_artist(
                fake_scryfall, many, "christopher rush", db=s
            )
            by_style = await recommender.recommend_by_style(
                fake_scryfall, many, showcase=True, db=s
            )
        assert [m.scryfall_id for m in by_artist.matched] == ["bolt-old"]
        assert (len(by_artist.unmatched), by_artist.skipped) == (40, [])
        assert [(m.scryfall_id, m.matched_criteria) for m in by_style.matched] == [
            ("bolt-new", ["showcase"])
        ]
        thumb = by_style.matched[0].image_small
        assert thumb is None or "/thumb/" in thumb


class TestArtCacheRefresh:
    async def test_new_scans_are_downloaded_again_but_bulk_regeneration_is_not(self, client):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, content=f"png-{len(calls)}".encode())

        cache = ArtCache(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        card = {**SIMPLE_CARD, "image_uris": {"png": "https://cards.scryfall.io/png/x.png"}}
        async with session_scope() as s:
            await printings.upsert_printing(s, card)
            await s.commit()

            async def fetch(updated_at):
                await s.execute(
                    update(PrintingCache)
                    .where(PrintingCache.scryfall_id == card["id"])
                    .values(image_updated_at=updated_at)
                )
                await s.commit()
                result = await cache.ensure_many(s, [(card["id"], "front")])
                return result[(card["id"], "front")]

            first = await fetch(None)
            assert (len(calls), first.bytes_size) == (1, 5)
            await fetch("2026-07-13T02:02:53Z")
            assert len(calls) == 1
            await fetch((datetime.now(UTC) + timedelta(days=1)).isoformat())
            assert len(calls) == 2
            await fetch((datetime.now(UTC) - timedelta(minutes=1)).isoformat())
            assert len(calls) == 2
        await cache.aclose()


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


async def test_collection_goes_through_the_scryfall_client(client, fake_scryfall):
    from mpc_forge.routes import collection

    collection._SETS_CACHE = None
    calls = []

    async def sets():
        calls.append("sets")
        return [
            {
                "code": "c21",
                "name": "Commander 2021",
                "set_type": "commander",
                "card_count": 2,
                "released_at": "2021-04-23",
            },
            {"code": "tc21", "name": "Tokens", "set_type": "token", "card_count": 5},
            {"code": "emp", "name": "Vacío", "set_type": "expansion", "card_count": 0},
        ]

    async def search_all(query, **params):
        calls.append((query, params))
        return [
            {
                "id": "sr-en",
                "oracle_id": "o",
                "name": "Sol Ring",
                "collector_number": "263",
                "image_uris": {"small": "s.jpg"},
            }
        ]

    fake_scryfall.sets = sets
    fake_scryfall.search_all = search_all
    listed = (await client.get("/api/collection/sets")).json()
    assert [s["code"] for s in listed] == ["c21"]
    await client.get("/api/collection/sets")
    cards = (await client.get("/api/collection/sets/c21/cards")).json()
    assert cards[0]["scryfall_id"] == "sr-en" and cards[0]["owned"] is False
    assert calls == ["sets", ("set:c21 -is:digital", {"unique": "prints", "order": "set"})]
    collection._SETS_CACHE = None
