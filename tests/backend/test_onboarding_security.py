from __future__ import annotations

import warnings

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from mpc_forge import middleware
from mpc_forge.services.system import whats_new

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def token_app(monkeypatch, fake_scryfall):
    warnings.filterwarnings("ignore")
    monkeypatch.setenv(middleware.ACCESS_TOKEN_ENV, "s3cret")
    monkeypatch.setenv(middleware.EXPOSED_ENV, "1")
    from mpc_forge.app import create_app

    app = create_app()
    app.state.scryfall = fake_scryfall
    return app


class TestAccessToken:
    async def test_requests_without_the_token_are_refused(self, client, token_app):
        async with AsyncClient(transport=ASGITransport(app=token_app), base_url=BASE) as ac:
            page = await ac.get("/")
            assert page.status_code == 401 and "?token=" in page.text
            api = await ac.get("/api/tour/")
            assert api.status_code == 401
            assert api.json() == {"detail": {"code": "access_token_required"}}
            wrong = await ac.get("/api/tour/", headers={"Authorization": "Bearer nope"})
            assert wrong.status_code == 401

    async def test_query_token_becomes_a_cookie(self, client, token_app):
        async with AsyncClient(transport=ASGITransport(app=token_app), base_url=BASE) as ac:
            r = await ac.get("/settings?token=s3cret&x=1")
            assert r.status_code == 303 and r.headers["location"] == "/settings?x=1"
            cookie = r.headers["set-cookie"]
            assert "mpc_forge_token=s3cret" in cookie and "HttpOnly" in cookie
            assert "samesite=strict" in cookie.lower()
            assert (await ac.get("/api/tour/")).status_code == 200

    async def test_bearer_header_is_accepted(self, client, token_app):
        async with AsyncClient(transport=ASGITransport(app=token_app), base_url=BASE) as ac:
            r = await ac.get("/api/tour/", headers={"Authorization": "Bearer s3cret"})
            assert r.status_code == 200

    async def test_debug_routes_are_hidden_when_exposed(self, client, token_app):
        headers = {"Authorization": "Bearer s3cret"}
        async with AsyncClient(transport=ASGITransport(app=token_app), base_url=BASE) as ac:
            assert (await ac.get("/api/debug/log/info", headers=headers)).status_code == 404
        assert (await client.get("/api/debug/log/info")).status_code == 200

    def test_cli_token_rules(self, monkeypatch):
        from mpc_forge.__main__ import _access_token

        monkeypatch.delenv(middleware.ACCESS_TOKEN_ENV, raising=False)
        assert _access_token("given") == "given"
        assert _access_token("off") is None
        generated = _access_token(None)
        assert generated and len(generated) >= 20
        monkeypatch.setenv(middleware.ACCESS_TOKEN_ENV, "from-env")
        assert _access_token(None) == "from-env"


class TestWhatsNew:
    async def test_first_run_is_silent_and_updates_are_announced(self, client, monkeypatch):
        from mpc_forge.db import session_scope

        async def notes(version, client=None):
            return f"Notes for {version}"

        monkeypatch.setattr(whats_new, "fetch_notes", notes)
        async with session_scope() as db:
            assert await whats_new.pending(db, "0.0.0") is None
            assert await whats_new.pending(db, "1.2.0") is None
            assert await whats_new.pending(db, "1.2.0") is None
            info = await whats_new.pending(db, "1.3.0")
            assert info == {
                "version": "1.3.0",
                "previous": "1.2.0",
                "notes": "Notes for 1.3.0",
                "url": f"{whats_new.RELEASES_URL}/tag/v1.3.0",
            }
            await whats_new.mark_seen(db, "1.3.0")
            assert await whats_new.pending(db, "1.3.0") is None

    async def test_endpoints_in_a_dev_build(self, client):
        assert (await client.get("/api/whats-new/")).json() is None
        assert (await client.post("/api/whats-new/seen")).status_code == 204

    async def test_notes_come_from_the_release(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v2.0.0"):
                return httpx.Response(200, json={"body": "## Added\n- things"})
            return httpx.Response(404, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert await whats_new.fetch_notes("2.0.0", http) == "## Added\n- things"
            assert await whats_new.fetch_notes("9.9.9", http) is None

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
            assert await whats_new.fetch_notes("2.0.0", http) is None


class TestOnboarding:
    async def test_checklist_until_dismissed(self, client):
        page = (await client.get("/")).text
        assert 'data-testid="onboarding"' in page and "onboardingPanel()" in page
        await client.post("/api/tour/seen", json={"tour": "onboarding"})
        assert 'data-testid="onboarding"' not in (await client.get("/")).text

    async def test_sample_deck(self, client):
        r = await client.post("/api/decks/import/sample", json={"name": "Ejemplo"})
        assert r.status_code == 200, r.text
        body = r.json()
        names = {c["name"] for c in body["deck"]["cards"]}
        assert {"Sol Ring", "Arcane Signet", "Command Tower"} <= names
        assert body["deck"]["name"] == "Ejemplo" and body["unresolved"]
        activity = (await client.get(f"/api/decks/{body['deck']['id']}/activity")).json()
        assert activity[0]["payload"]["source"] == "sample"

    async def test_sample_deck_failure_is_reported(self, client, fake_scryfall):
        from unittest.mock import AsyncMock

        fake_scryfall.collection = AsyncMock(side_effect=RuntimeError("offline"))
        r = await client.post("/api/decks/import/sample", json={})
        assert r.status_code == 502 and r.json()["detail"]["code"] == "sample_deck_failed"
