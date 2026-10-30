from __future__ import annotations

import httpx
import pytest

from mpc_forge.clients import import_sites
from mpc_forge.clients.import_sites import base
from mpc_forge.clients.import_sites.archidekt import ArchidektSite
from mpc_forge.clients.import_sites.base import ImportSiteError, InvalidURLError
from mpc_forge.clients.import_sites.cubecobra import CubeCobraSite
from mpc_forge.clients.import_sites.deckstats import DeckstatsSite
from mpc_forge.clients.import_sites.mtggoldfish import MTGGoldfishSite
from mpc_forge.clients.import_sites.scryfall import ScryfallSite
from mpc_forge.clients.import_sites.tappedout import TappedOutSite

ARCHIDEKT = {
    "name": "Atraxa Superfriends",
    "cards": [
        {"quantity": 1, "categories": ["Commander"], "card": {"oracleCard": {"name": "Atraxa"}}},
        {"quantity": 2, "categories": ["Ramp"], "card": {"oracleCard": {"name": "Sol Ring"}}},
        {"quantity": 1, "categories": ["Sideboard"], "card": {"oracleCard": {"name": "Opt"}}},
        {"quantity": 1, "categories": ["Maybeboard"], "card": {"oracleCard": {"name": "Bolt"}}},
        {"quantity": 1, "categories": [], "card": {"oracleCard": {}}},
    ],
}

ROUTES: dict[str, httpx.Response] = {
    "https://archidekt.com/api/decks/123/": httpx.Response(200, json=ARCHIDEKT),
    "https://archidekt.com/api/decks/999/": httpx.Response(200, json={"cards": []}),
    "https://tappedout.net/mtg-decks/my-cool-deck/?fmt=txt": httpx.Response(
        200, content=b"1 Sol Ring\r\nSideboard:\r\n1 Opt\r\n"
    ),
    "https://cubecobra.com/cube/download/plaintext/abc": httpx.Response(
        200, text="# comment\n1 Sol Ring\n1 Opt\n"
    ),
    "https://cubecobra.com/cube/api/cubejson/abc": httpx.Response(200, json={"name": " My Cube "}),
    "https://www.mtggoldfish.com/deck/download/42": httpx.Response(200, text="4 Bolt\n\n2 Opt\n"),
    "https://deckstats.net/decks/1/2-burn-deck?export_txt=1": httpx.Response(
        200, text="  // Sideboard\n1 Opt\n4 Bolt\n"
    ),
    "https://api.scryfall.com/decks/abc-123/export/text": httpx.Response(
        200, text="1 Sol Ring\n// Sideboard\n1 Opt\n"
    ),
}


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        key = url.split("?", 1)[0] if "cubecobra.com/cube/download" in url else url
        if key in ROUTES:
            return ROUTES[key]
        return httpx.Response(404, text="not found")

    real = httpx.AsyncClient

    class MockClient(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.pop("verify", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(base.httpx, "AsyncClient", MockClient)
    return seen


class TestArchidekt:
    async def test_boards_and_name(self):
        url = "https://archidekt.com/decks/123/atraxa"
        text = await ArchidektSite.retrieve_card_list(url)
        assert text.splitlines() == [
            "//Commanders",
            "1 Atraxa",
            "",
            "//Mainboard",
            "2 Sol Ring",
            "",
            "//Sideboard",
            "1 Opt",
            "",
            "//Maybeboard",
            "1 Bolt",
        ]
        assert await ArchidektSite.retrieve_deck_name(url) == "Atraxa Superfriends"

    async def test_errors(self):
        with pytest.raises(InvalidURLError):
            await ArchidektSite.retrieve_card_list("https://archidekt.com/users/1")
        with pytest.raises(ImportSiteError):
            await ArchidektSite.retrieve_card_list("https://archidekt.com/decks/999/x")


class TestTextSites:
    async def test_tappedout(self):
        url = "https://tappedout.net/mtg-decks/my-cool-deck/"
        text = await TappedOutSite.retrieve_card_list(url)
        assert [line.strip() for line in text.splitlines()] == [
            "1 Sol Ring",
            "//Sideboard",
            "1 Opt",
        ]
        assert await TappedOutSite.retrieve_deck_name(url) == "My Cool Deck"
        assert await TappedOutSite.retrieve_deck_name("https://tappedout.net/mtg-decks/") is None
        with pytest.raises(InvalidURLError):
            await TappedOutSite.retrieve_card_list("https://tappedout.net/")

    async def test_cubecobra(self):
        url = "https://cubecobra.com/cube/list/abc"
        assert await CubeCobraSite.retrieve_card_list(url) == "1 Sol Ring\n1 Opt"
        assert await CubeCobraSite.retrieve_deck_name(url) == "My Cube"
        assert await CubeCobraSite.retrieve_deck_name("https://cubecobra.com/cube/list/zzz") is None
        assert await CubeCobraSite.retrieve_deck_name("https://cubecobra.com/") is None
        with pytest.raises(InvalidURLError):
            await CubeCobraSite.retrieve_card_list("https://cubecobra.com/")

    async def test_mtggoldfish(self):
        assert (
            await MTGGoldfishSite.retrieve_card_list("https://www.mtggoldfish.com/deck/42#paper")
            == "4 Bolt\n\n2 Opt"
        )
        archetype = "https://www.mtggoldfish.com/archetype/modern-burn#paper"
        assert await MTGGoldfishSite.retrieve_deck_name(archetype) == "Modern Burn"
        assert (
            await MTGGoldfishSite.retrieve_deck_name("https://www.mtggoldfish.com/deck/42") is None
        )
        with pytest.raises(InvalidURLError):
            await MTGGoldfishSite.retrieve_card_list("https://www.mtggoldfish.com/")

    async def test_deckstats(self):
        url = "https://deckstats.net/decks/1/2-burn-deck"
        assert await DeckstatsSite.retrieve_card_list(url) == "//Sideboard\n1 Opt\n4 Bolt"
        assert await DeckstatsSite.retrieve_deck_name(url) == "Burn Deck"
        assert await DeckstatsSite.retrieve_deck_name("https://deckstats.net/decks/1") is None
        with pytest.raises(InvalidURLError):
            await DeckstatsSite.retrieve_card_list("https://deckstats.net/users/1")

    async def test_scryfall(self):
        url = "https://scryfall.com/@me/decks/abc-123"
        assert await ScryfallSite.retrieve_card_list(url) == "1 Sol Ring\n//Sideboard\n1 Opt"
        with pytest.raises(InvalidURLError):
            await ScryfallSite.retrieve_card_list("https://scryfall.com/search")

    async def test_http_errors_surface(self):
        with pytest.raises(httpx.HTTPStatusError):
            await MTGGoldfishSite.retrieve_card_list("https://www.mtggoldfish.com/deck/404")


class TestRegistry:
    @pytest.mark.parametrize(
        ("url", "site"),
        [
            ("https://www.archidekt.com/decks/1", ArchidektSite),
            ("https://mtggoldfish.com/deck/1", MTGGoldfishSite),
            ("https://example.com/decks/1", None),
            ("not a url", None),
        ],
    )
    def test_resolve(self, url, site):
        assert import_sites.resolve_site(url) is site

    def test_supported_sites_listing(self):
        names = {s["name"] for s in import_sites.list_supported_sites()}
        assert {"Archidekt", "Moxfield", "Scryfall", "TappedOut"} <= names

    def test_subclasses_must_declare_their_identity(self):
        with pytest.raises(TypeError):
            type("Broken", (base.ImportSite,), {"key": "", "name": "x", "host_names": ("x",)})
        with pytest.raises(TypeError):
            type("Broken", (base.ImportSite,), {"key": "x", "name": "", "host_names": ("x",)})
        with pytest.raises(TypeError):
            type("Broken", (base.ImportSite,), {"key": "x", "name": "x", "host_names": ()})

    async def test_request_needs_a_host(self):
        class NoHost(base.ImportSite, register=False):
            pass

        with pytest.raises(ImportSiteError):
            await NoHost.request("/x")
        with pytest.raises(NotImplementedError):
            await NoHost.retrieve_card_list("x")
        assert await NoHost.retrieve_deck_name("x") is None

    def test_payload_cache_expires_and_evicts(self, monkeypatch):
        cache = base.PayloadCache(ttl_seconds=10, max_entries=2)
        clock = [100.0]
        monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
        cache.put("a", {"n": 1})
        clock[0] += 1
        cache.put("b", {"n": 2})
        cache.put("c", {"n": 3})
        assert cache.get("a") is None and cache.get("b") == {"n": 2}
        clock[0] += 20
        assert cache.get("c") is None
        cache.pop("missing")

    def test_slug_titles(self):
        assert base._slug_to_title("my_cool-deck") == "My Cool Deck"
        assert base._slug_to_title("123-456") == ""
