from __future__ import annotations

import asyncio
import json
import logging
import sys
import warnings
from pathlib import Path

import pytest

from mpc_forge.middleware import _hostname, is_same_origin
from mpc_forge.services.system import settings as settings_service
from mpc_forge.services.system.logging_setup import RedactSecretsFilter, redact
from mpc_forge.utils import event_loop

SNAPSHOT = Path(__file__).resolve().parents[1] / "data_openapi_snapshot.json"
FAKE_KEY = "AIzaSyD-ExampleKeyForTestsOnly-0123456789"
LITERAL_DECK_ROUTES = {
    "/api/decks/_/search-cards": "/api/decks/_/search-cards?q=sol",
    "/api/decks/_/with-activity": "/api/decks/_/with-activity",
    "/api/decks/_/undoable-kinds": "/api/decks/_/undoable-kinds",
    "/api/decks/_/supported-langs": "/api/decks/_/supported-langs",
    "/api/decks/_/autocomplete": "/api/decks/_/autocomplete?q=sol",
}


@pytest.fixture(scope="module")
def openapi() -> dict:
    warnings.filterwarnings("ignore")
    from mpc_forge.app import create_app

    return create_app().openapi()


class TestApiSurface:
    def test_routes_and_parameters_match_the_snapshot(self, openapi):
        current = {
            f"{method.upper()} {path}": sorted(
                (p["name"], p["in"], p.get("required", False)) for p in op.get("parameters", [])
            )
            for path, methods in openapi["paths"].items()
            for method, op in methods.items()
        }
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        missing = sorted(set(snapshot) - set(current))
        assert not missing, "Endpoints desaparecidos:\n" + "\n".join(missing)
        changed = [
            key
            for key in sorted(set(snapshot) & set(current))
            if [tuple(p) for p in snapshot[key]] != current[key]
        ]
        assert not changed, f"Parámetros alterados: {changed}"

    async def test_literal_deck_routes_are_not_captured_by_deck_id(self, client, openapi):
        for route, url in LITERAL_DECK_ROUTES.items():
            assert route in openapi["paths"], route
            response = await client.get(url)
            assert response.status_code not in (422, 500), (route, response.status_code)

    async def test_static_lookups_are_cacheable(self, client):
        for url in ("/api/decks/_/supported-langs", "/api/decks/_/undoable-kinds"):
            r = await client.get(url)
            assert r.status_code == 200 and "max-age" in r.headers["cache-control"].lower()

    async def test_versioned_static_assets_are_immutable(self, client):
        versioned = await client.get("/static/js/core/app.js?v=1")
        assert "immutable" in versioned.headers["cache-control"]
        plain = await client.get("/static/js/core/app.js")
        assert plain.headers["cache-control"] == "public, max-age=86400"


class TestLocalhostGuard:
    async def test_only_local_hosts_are_served(self, client):
        for host in ("127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:9999", "[::1]:8765"):
            r = await client.get("/api/settings/", headers={"host": host})
            assert r.status_code == 200, host
        for host in (
            "evil.com",
            "attacker.example",
            "rebind.attacker-dns.example",
            "127.0.0.1.evil.com",
        ):
            r = await client.get("/api/settings/", headers={"host": host})
            assert r.status_code == 400 and "definitions" not in r.text, host
        assert _hostname("[::1]:8765") == "[::1]"
        assert _hostname("127.0.0.1:8765") == "127.0.0.1"
        assert _hostname("LocalHost") == "localhost"

    async def test_cross_site_writes_are_rejected(self, client, deck):
        url = f"/api/decks/{deck['id']}"
        same = await client.patch(
            url, json={"name": "A"}, headers={"sec-fetch-site": "same-origin"}
        )
        cross = await client.patch(
            url, json={"name": "B"}, headers={"sec-fetch-site": "cross-site"}
        )
        read = await client.get(url, headers={"sec-fetch-site": "cross-site"})
        assert (same.status_code, cross.status_code, read.status_code) == (200, 403, 200)

    async def test_language_switch_never_redirects_off_site(self, client):
        async def switch(referer):
            r = await client.post(
                "/set-lang",
                data={"lang": "en"},
                headers={"referer": referer},
                follow_redirects=False,
            )
            return r.headers["location"]

        assert await switch("https://evil.example/phishing") == "/"
        assert (await switch("http://127.0.0.1:8765/collection")).endswith("/collection")

        class _Req:
            class url:
                netloc = "127.0.0.1:8765"

        assert is_same_origin(_Req(), "/decks/1") is True
        assert is_same_origin(_Req(), "//evil.com/x") is False
        assert is_same_origin(_Req(), "https://evil.com/x") is False


class TestSecrets:
    async def test_api_key_is_write_only(self, client):
        saved = await client.put("/api/settings/", json={"values": {"google_api_key": FAKE_KEY}})
        assert saved.status_code == 200 and FAKE_KEY not in saved.text
        body = (await client.get("/api/settings/")).json()
        assert FAKE_KEY not in json.dumps(body)
        assert body["values"]["google_api_key"] == "" and "google_api_key" in body["secrets_set"]
        by_key = {d["key"]: d for d in body["definitions"]}
        assert (
            by_key["google_api_key"]["secret"] is True and by_key["ssl_insecure"]["secret"] is False
        )

        await client.put("/api/settings/", json={"values": {"google_api_key": ""}})
        assert "google_api_key" in (await client.get("/api/settings/")).json()["secrets_set"]
        await client.put(
            "/api/settings/", json={"values": {"google_api_key": settings_service.SECRET_CLEAR}}
        )
        assert "google_api_key" not in (await client.get("/api/settings/")).json()["secrets_set"]

    def test_credentials_are_redacted_from_logs(self):
        for leaky in (
            f"GET https://www.googleapis.com/drive/v3/files?q=x&key={FAKE_KEY}",
            f"Authorization: Bearer {FAKE_KEY}",
            f"clave suelta {FAKE_KEY} en medio del texto",
            "https://api.example/x?access_token=abc123def&other=1",
        ):
            cleaned = redact(leaky)
            assert FAKE_KEY not in cleaned and "abc123def" not in cleaned
            assert "[REDACTED]" in cleaned
        harmless = "Indexado completo: 1234 ficheros en 5 carpetas"
        assert redact(harmless) == harmless

        record = logging.LogRecord(
            "test",
            logging.WARNING,
            __file__,
            1,
            "Fallo pidiendo %s",
            (f"https://x/y?key={FAKE_KEY}",),
            None,
        )
        RedactSecretsFilter().filter(record)
        assert FAKE_KEY not in record.getMessage()
        try:
            raise ValueError(f"peticion a https://x/y?key={FAKE_KEY} fallida")
        except ValueError:
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1, "fallo", (), sys.exc_info()
            )
        RedactSecretsFilter().filter(record)
        assert FAKE_KEY not in (record.exc_text or "")


def test_windows_connection_resets_are_silenced_but_real_errors_are_not(monkeypatch):
    def context(exc, handle="<Handle _ProactorBasePipeTransport._call_connection_lost()>"):
        return {"exception": exc, "handle": handle, "message": "Exception in callback"}

    assert event_loop.is_benign_connection_reset(context(ConnectionResetError(10054, "reset")))
    assert not event_loop.is_benign_connection_reset(context(ValueError("boom")))
    assert not event_loop.is_benign_connection_reset(
        context(ConnectionResetError(), handle="<Handle something_else()>")
    )

    monkeypatch.setattr(event_loop.sys, "platform", "win32")
    loop = asyncio.new_event_loop()
    seen = []
    loop.set_exception_handler(lambda _l, ctx: seen.append(ctx["exception"]))
    try:
        event_loop.silence_windows_connection_resets(loop)
        loop.call_exception_handler(context(ConnectionResetError(10054, "reset")))
        loop.call_exception_handler(context(RuntimeError("real problem")))
    finally:
        loop.close()
    assert [type(e) for e in seen] == [RuntimeError]
