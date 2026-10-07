from __future__ import annotations

from mpc_forge.services.system.i18n import get_translations
from tests.frontend._support import HTML


class TestLanding:
    async def test_renders_without_data(self, client):
        r = await client.get("/", headers=HTML)
        assert r.status_code == 200
        assert 'class="lp-hero"' in r.text
        assert r.text.count("lp-card--sample") == 5
        assert "landingImport()" in r.text and "lp-recent" not in r.text
        assert "Moxfield" in r.text and "Archidekt" in r.text
        assert "unresolvedModal.open" in r.text

    async def test_shows_recent_decks_and_plurals(self, client, deck):
        r = await client.get("/", headers=HTML)
        assert f'href="/decks/{deck["id"]}"' in r.text
        assert f'href="/decks/{deck["id"]}/pdf"' in r.text
        assert deck["name"] in r.text
        assert "1 mazo y 0 cartas marcadas en tu colección" in r.text

        client.cookies.set("lang", "en")
        r = await client.get("/", headers=HTML)
        assert "From decklist to printed sheet." in r.text
        assert "1 deck and 0 marked cards in your collection" in r.text

    async def test_workshop_status_survives_a_broken_service(self, client, monkeypatch):
        from mpc_forge.services.indexing import gdrive_search

        async def boom(db):
            raise RuntimeError("índice a medio migrar")

        monkeypatch.setattr(gdrive_search, "stats", boom)
        r = await client.get("/", headers=HTML)
        assert r.status_code == 200 and "lp-status" in r.text


class TestShell:
    async def test_deck_library_and_editor_navigation(self, client, deck):
        r = await client.get("/decks", headers=HTML)
        assert r.status_code == 200 and "importPanel()" in r.text
        assert f'data-deck-cover="{deck["id"]}"' in r.text or deck["name"] in r.text

        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert 'href="/decks" class="text-fg-muted' in r.text
        assert r.text.count('class="nav-indicator"') == 1
        assert r.text.index('class="nav-indicator"') > r.text.index('href="/decks"\n')

    async def test_sidebar_is_sticky_and_search_escapes_the_nav(self, client):
        r = await client.get("/", headers=HTML)
        aside_classes = r.text.split("<aside", 1)[1].split('class="', 1)[1].split('"', 1)[0].split()
        assert {"sticky", "top-0", "h-screen"} <= set(aside_classes)
        teleport = r.text.index('<template x-teleport="body">')
        dropdown = r.text.index('id="global-search-results"')
        assert teleport < dropdown < r.text.index("</nav>")
        assert "fixed z-[60]" in r.text[dropdown : dropdown + 600]

    async def test_settings_page_renders_its_sections(self, client):
        r = await client.get("/settings", headers=HTML)
        assert r.status_code == 200
        for needle in (
            "settingsShell()",
            "sticky top-0",
            'x-model="searchQuery"',
            "showDrivesModal",
            "Red y conexión",
            "MPC Autofill",
            "ssl_insecure",
            "resetAll",
        ):
            assert needle in r.text, needle


class TestNotFoundPage:
    async def test_browser_navigation_gets_the_styled_page(self, client):
        r = await client.get("/esto-no-existe", headers=HTML)
        assert r.status_code == 404 and "text/html" in r.headers["content-type"]
        assert 'class="nf"' in r.text
        code = r.text.split("<code>", 1)[1].split("</code>", 1)[0]
        assert code.replace("<wbr>", "") == "/esto-no-existe"

        long_path = "/" + "x" * 120
        r = await client.get(long_path, headers=HTML)
        assert "x" * 120 not in r.text.split("<code>", 1)[1].split("</code>", 1)[0]

        r = await client.get("/%3Cscript%3Ealert(1)%3C%2Fscript%3E", headers=HTML)
        assert r.status_code == 404 and "<script>alert(1)" not in r.text

    async def test_missing_deck_pages_use_the_deck_copy(self, client):
        r = await client.get("/decks/999999", headers=HTML)
        assert r.status_code == 404 and get_translations("es").nf_deck_body in r.text
        for suffix in ("pdf", "proof"):
            r = await client.get(f"/decks/999999/{suffix}", headers=HTML)
            assert r.status_code == 404 and 'class="nf"' in r.text

    async def test_non_page_requests_keep_their_404(self, client):
        r = await client.get("/api/no-such-endpoint", headers=HTML)
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
        r = await client.get("/esto-no-existe")
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
        r = await client.get("/static/no-such-file.js", headers=HTML)
        assert r.status_code == 404 and 'class="nf"' not in r.text
        r = await client.post("/esto-no-existe", headers=HTML)
        assert r.status_code in (404, 405) and 'class="nf"' not in r.text
