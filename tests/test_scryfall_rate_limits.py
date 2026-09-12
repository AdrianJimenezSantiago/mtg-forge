"""Rate limit del cliente de Scryfall e import con caché local.

Los tests del cliente usan un servidor falso (``httpx.MockTransport``) que
aplica las mismas reglas que Scryfall, con los tiempos escalados para que la
suite siga siendo rápida:

* endpoints pesados (search/named/random/collection): un hueco mínimo entre
  peticiones; si se viola, 429.
* resto: un hueco mínimo más pequeño; si se viola, 429.
* tras un 429, el servidor sigue devolviendo 429 durante el enfriamiento.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC

import httpx
import pytest

from mpc_forge.clients.scryfall import HEAVY_PATHS, ScryfallClient

# Escala de tiempos: fracción de los valores reales. A escalas menores el
# jitter del event loop (pocos ms) se come el margen de seguridad del cliente y
# el test mediría el scheduler de Python, no la lógica de reserva.
#
# En Windows hay que multiplicar la escala. El temporizador por defecto del
# sistema tiene una granularidad de ~15,6 ms, así que con GENERAL = 20 ms y un
# 5 % de tolerancia el margen real era de UN milisegundo: dos peticiones
# separadas correctamente podían aterrizar en el mismo tick del reloj y el
# servidor falso las contaba como violación, devolviendo 429 y haciendo fallar
# el test por un problema de resolución del reloj, no del código.
#
# Con x5 los huecos quedan muy por encima de esa granularidad. Cuesta unos
# segundos más en Windows y es el único sitio de la suite donde la plataforma
# cambia una constante.
# x5 en Windows (granularidad del temporizador ~15,6 ms) y x2 en el resto.
# El x2 no es cosmético: con GENERAL a 20 ms el margen absoluto eran 2 ms, y
# el test fallaba de forma intermitente al ejecutarse dentro de la suite
# completa —donde hay contención de CPU— aunque pasara siempre en aislado.
# Un test que solo falla acompañado es peor que uno lento.
_SCALE = 5 if sys.platform == "win32" else 2

HEAVY = 0.10 * _SCALE       # real: 0.5 s
GENERAL = 0.02 * _SCALE     # real: 0.1 s
COOLDOWN = 0.3 * _SCALE     # real: 30 s

# Presupuesto de ruido del planificador, en segundos absolutos.
#
# El cliente reserva su hueco correctamente, pero entre la reserva y el
# momento en que la corrutina llega a enviar de verdad puede pasar un rato: si
# la petición N se retrasa 40 ms y la N+1 no, ambas llegan al servidor más
# juntas de lo que el cliente pretendía. Eso NO es un fallo del limitador.
#
# Expresarlo como milisegundos absolutos en vez de como un porcentaje del
# hueco dice lo que de verdad se está tolerando, y no se descuadra si mañana
# se cambia la escala de tiempos. Una violación real es de espaciado ~0, muy
# por debajo de este margen, así que el test sigue detectándolas.
JITTER = 0.06 if sys.platform == "win32" else 0.008


class FakeScryfall:
    """Servidor que aplica las reglas de rate limit de Scryfall."""

    def __init__(self, *, heavy: float = HEAVY, general: float = GENERAL,
                 cooldown: float = COOLDOWN, force_429: int = 0) -> None:
        self.heavy = heavy
        self.general = general
        self.cooldown = cooldown
        self.force_429 = force_429
        self.last_heavy = -1e9
        self.last_general = -1e9
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
            self.blocked_until = now + self.cooldown
            return self._limited()
        # Se descuenta JITTER (ver arriba) del hueco exigido.
        if path in HEAVY_PATHS:
            if now - self.last_heavy < self.heavy - JITTER:
                self.violations += 1
                self.blocked_until = now + self.cooldown
                return self._limited()
            self.last_heavy = now
        if now - self.last_general < self.general - JITTER:
            self.violations += 1
            self.blocked_until = now + self.cooldown
            return self._limited()
        self.last_general = now
        return self._respond(request)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/cards/collection":
            import json
            idents = json.loads(request.content)["identifiers"]
            data = [{"id": f"id-{i.get('name') or i.get('id')}", "name": i.get("name", "x")}
                    for i in idents]
            return httpx.Response(200, json={"data": data})
        if path == "/cards/search":
            page = int(request.url.params.get("page", "1"))
            has_more = page < 3
            next_page = (
                f"https://api.scryfall.com/cards/search?q=x&page={page + 1}" if has_more else None
            )
            return httpx.Response(200, json={
                "data": [{"id": f"p{page}"}], "has_more": has_more, "next_page": next_page,
            })
        if path == "/cards/autocomplete":
            return httpx.Response(200, json={"data": ["Sol Ring"]})
        return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})


def make_client(server: FakeScryfall) -> ScryfallClient:
    http = httpx.AsyncClient(
        base_url="https://api.scryfall.com",
        transport=httpx.MockTransport(server.handler),
    )
    return ScryfallClient(
        http, general_interval=GENERAL * 1.1, heavy_interval=HEAVY * 1.1, cooldown=COOLDOWN,
    )


class TestTieredLimits:
    async def test_mixed_concurrent_burst_never_trips_the_limit(self):
        """Cinco "imports" concurrentes mezclando endpoints: cero 429."""
        server = FakeScryfall()
        sc = make_client(server)

        async def one_import(n: int) -> None:
            await sc.collection([{"name": f"card-{n}-{i}"} for i in range(150)])
            await asyncio.gather(*(sc.by_id(f"{n}-{i}") for i in range(5)))
            await sc.named(f"Commander {n}")

        await asyncio.gather(*(one_import(n) for n in range(5)))
        assert server.violations == 0
        assert server.rate_limited == 0
        await sc.aclose()

    async def test_heavy_endpoints_are_spaced_at_the_heavy_interval(self):
        server = FakeScryfall()
        sc = make_client(server)
        start = time.monotonic()
        await asyncio.gather(*(sc.named(f"card {i}") for i in range(5)))
        elapsed = time.monotonic() - start
        # 5 peticiones → 4 huecos pesados como mínimo.
        assert elapsed >= 4 * HEAVY
        assert server.rate_limited == 0
        await sc.aclose()

    async def test_light_endpoints_are_not_slowed_by_the_heavy_lane(self):
        server = FakeScryfall()
        sc = make_client(server)
        start = time.monotonic()
        await asyncio.gather(*(sc.by_id(f"id-{i}") for i in range(5)))
        assert time.monotonic() - start < 4 * HEAVY
        await sc.aclose()

    async def test_pagination_goes_through_the_heavy_lane(self):
        server = FakeScryfall()
        sc = make_client(server)
        cards = await sc.search_all("x")
        assert [c["id"] for c in cards] == ["p1", "p2", "p3"]
        assert server.violations == 0
        await sc.aclose()


class TestRateLimitCooldown:
    async def test_429_pauses_every_request_and_retries_once(self):
        server = FakeScryfall(force_429=1)
        sc = make_client(server)
        start = time.monotonic()
        results = await asyncio.gather(*(sc.by_id(f"id-{i}") for i in range(4)))
        elapsed = time.monotonic() - start
        assert all(r.get("id") for r in results)
        assert elapsed >= COOLDOWN
        # Solo el 429 forzado: nadie siguió disparando durante el bloqueo.
        assert server.rate_limited == 1
        assert sc.stats["rate_limited"] == 1
        await sc.aclose()

    async def test_gives_up_after_a_second_429(self):
        server = FakeScryfall(force_429=2)
        sc = make_client(server)
        with pytest.raises(httpx.HTTPStatusError):
            await sc.by_id("abc")
        assert server.rate_limited == 2
        await sc.aclose()

    async def test_autocomplete_fails_fast_during_cooldown(self):
        server = FakeScryfall()
        sc = make_client(server)
        sc._start_cooldown(10)
        start = time.monotonic()
        assert await sc.autocomplete("sol") == []
        assert time.monotonic() - start < 0.1
        assert server.calls == []
        await sc.aclose()


class TestDeduplication:
    async def test_concurrent_identical_gets_share_one_request(self):
        server = FakeScryfall()
        sc = make_client(server)
        results = await asyncio.gather(*(sc.by_id("same") for _ in range(6)))
        assert all(r == {"id": "same"} for r in results)
        assert server.calls.count("/cards/same") == 1
        assert sc.stats["deduplicated"] == 5
        await sc.aclose()

    async def test_cancelling_one_caller_does_not_break_the_others(self):
        server = FakeScryfall()
        sc = make_client(server)
        t1 = asyncio.create_task(sc.search_all("x"))
        t2 = asyncio.create_task(sc.search_all("x"))
        await asyncio.sleep(0)
        t1.cancel()
        assert len(await t2) == 3
        with pytest.raises(asyncio.CancelledError):
            await t1
        await sc.aclose()

    async def test_collection_sends_repeated_identifiers_once(self):
        server = FakeScryfall()
        sc = make_client(server)
        idents = [{"name": "Sol Ring"}] * 80 + [{"name": "Arcane Signet"}]
        out = await sc.collection(idents)
        # 81 identificadores, 2 únicos → 1 petición en vez de 2.
        assert server.calls.count("/cards/collection") == 1
        assert len(out) == 2
        await sc.aclose()


# ---------------------------------------------------------------------------
# Import: resolución desde caché local
# ---------------------------------------------------------------------------

class _CountingScryfall:
    """Envuelve el fake de conftest contando identificadores enviados."""

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
    from mpc_forge.services import deck_service
    deck_service._name_memo.clear()
    deck_service._prints_complete.clear()
    return _CountingScryfall(fake_scryfall)


class TestCacheFirstImport:
    TEXT = "1 Sol Ring\n1 Command Tower\n1 Arcane Signet"

    async def _import(self, scryfall, text=TEXT):
        from mpc_forge.db import session_scope
        from mpc_forge.services import deck_service
        async with session_scope() as db:
            deck, unresolved = await deck_service.import_from_plaintext(
                db, scryfall, "Deck", text,
            )
            return deck.id, unresolved

    async def test_second_import_of_same_list_hits_no_network(self, counting):
        await self._import(counting)
        assert len(counting.collection_calls) == 1
        await self._import(counting)
        await self._import(counting)
        assert len(counting.collection_calls) == 1

    async def test_only_unknown_cards_are_queried(self, counting):
        await self._import(counting)
        await self._import(counting, self.TEXT + "\n1 Lightning Bolt")
        assert counting.collection_calls[-1] == [{"name": "Lightning Bolt"}]

    async def test_imports_by_id_use_the_cache(self, counting):
        from mpc_forge.db import session_scope
        from mpc_forge.services import deck_service
        await self._import(counting)
        entries = [{"name": "Sol Ring", "quantity": 1, "scryfall_id": "sr-en",
                    "role": "mainboard"}]
        async with session_scope() as db:
            out = await deck_service.resolve_cards(db, counting, entries)
        assert out[0]["resolved"] and out[0]["scryfall_id"] == "sr-en"
        assert len(counting.collection_calls) == 1

    async def test_stale_cache_rows_are_refreshed(self, counting):
        from datetime import datetime, timedelta

        from sqlalchemy import update

        from mpc_forge.db import session_scope
        from mpc_forge.models import PrintingCache
        await self._import(counting)
        async with session_scope() as db:
            await db.execute(update(PrintingCache).values(
                fetched_at=datetime.now(UTC) - timedelta(days=30)))
            await db.commit()
        await self._import(counting)
        assert len(counting.collection_calls) == 2

    async def test_front_face_name_resolves_a_dfc(self, counting, fake_scryfall):
        dfc = {
            "id": "dos-en", "oracle_id": "oracle-delver", "set": "isd",
            "collector_number": "51", "lang": "en", "layout": "transform",
            "name": "Delver of Secrets // Insectile Aberration",
            "card_faces": [
                {"name": "Delver of Secrets", "image_uris": {"png": "https://x.test/f.png"}},
                {"name": "Insectile Aberration", "image_uris": {"png": "https://x.test/b.png"}},
            ],
        }

        async def collection(idents):
            return [dfc] if any(i.get("name") == "Delver of Secrets" for i in idents) else []

        counting.inner.collection = collection
        _, unresolved = await self._import(counting, "1 Delver of Secrets")
        assert unresolved == []


class TestPrintsPreloadCache:
    async def test_single_printing_card_is_fetched_only_once(self, counting):
        from mpc_forge.db import session_scope
        from mpc_forge.services import deck_service
        await TestCacheFirstImport()._import(counting)
        for _ in range(3):
            async with session_scope() as db:
                await deck_service.fetch_printings_for_oracle(
                    db, counting, "oracle-command-tower")
        assert counting.prints_calls == ["oracle-command-tower"]


class TestTieredRateLimiter:
    """Lógica de reserva, sin dormir: se congela el reloj."""

    @pytest.fixture
    def frozen(self, monkeypatch):
        from mpc_forge.services import rate_limiter
        clock = {"now": 1000.0}
        monkeypatch.setattr(rate_limiter.time, "monotonic", lambda: clock["now"])
        return clock

    def test_heavy_reservations_keep_heavy_spacing_even_with_light_traffic(self, frozen):
        from mpc_forge.services.rate_limiter import TieredRateLimiter
        lim = TieredRateLimiter(general=0.1, heavy=0.5)
        order = [True, False, False, False, False, False, True, False, True]
        slots = [(h, lim.reserve(h)) for h in order]
        heavy = sorted(t for h, t in slots if h)
        every = sorted(t for _, t in slots)
        assert all(b - a >= 0.5 - 1e-9 for a, b in zip(heavy, heavy[1:]))
        assert all(b - a >= 0.1 - 1e-9 for a, b in zip(every, every[1:]))

    def test_light_requests_fill_gaps_between_future_heavy_slots(self, frozen):
        from mpc_forge.services.rate_limiter import TieredRateLimiter
        lim = TieredRateLimiter(general=0.1, heavy=0.5)
        now = frozen["now"]
        assert lim.reserve(True) == now
        assert lim.reserve(True) == pytest.approx(now + 0.5)
        # Una ligera no espera a la segunda pesada: entra en el hueco.
        assert lim.reserve(False) == pytest.approx(now + 0.1)


class TestBatchedPreload:
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

    async def test_preload_batches_uncached_cards(self, counting, deck, monkeypatch):
        from mpc_forge.services import deck_service, preloader
        batches: list[list[str]] = []

        async def prints_by_oracle_ids(oids):
            batches.append(list(oids))
            return []

        counting.prints_by_oracle_ids = prints_by_oracle_ids
        monkeypatch.setattr(deck_service, "PRINTS_BATCH_SIZE", 2)
        state = await preloader.start(deck["id"], counting)
        await state.task
        assert sorted(len(b) for b in batches) == [1, 2]      # 3 cartas → 2 búsquedas
        assert state.done == state.total == 3

        # Segunda apertura del mazo: todo en memoria, cero búsquedas.
        batches.clear()
        state = await preloader.start(deck["id"], counting)
        await state.task
        assert batches == []
        assert state.done == state.total == 3


class TestSplitXmlRegression:
    async def test_split_xml_with_print_runs_does_not_crash(self, client, deck, monkeypatch):
        """``build-split-xml`` con ``create_runs`` usaba ``cfg`` sin importarlo → 500."""
        from pathlib import Path

        from mpc_forge.routes import export
        from mpc_forge.services.xml_generator import DeckCardResolved, XMLBuildResult

        async def fake_resolve(db, scryfall, art_cache, deck_obj):
            return [DeckCardResolved(name=c.name, quantity=c.quantity, scryfall_id=c.scryfall_id,
                                     front_path=Path("front.png")) for c in deck_obj.cards]

        def fake_build(*, cards, output_path, **_):
            return XMLBuildResult(xml_path=output_path, total_cards=sum(c.quantity for c in cards))

        monkeypatch.setattr(export, "resolve_deck_for_xml", fake_resolve)
        monkeypatch.setattr(export, "build_xml", fake_build)
        r = await client.post(f"/api/decks/{deck['id']}/build-split-xml",
                              json={"create_runs": True})
        assert r.status_code == 200, r.text
        assert len(r.json()["run_ids"]) == 1
