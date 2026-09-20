"""Landing en ``/``, biblioteca en ``/decks`` y página 404 propia.

Lo que se protege aquí:

* La landing es la puerta de entrada: tiene que responder aunque no haya
  mazos, índice de arte ni volcado de Scryfall.
* La biblioteca se movió de ``/`` a ``/decks``; los enlaces de vuelta del
  editor dependen de ello.
* El 404 con estilo solo debe servirse a navegaciones del navegador. La API,
  los estáticos y cualquier cliente que no pida HTML siguen recibiendo el
  JSON de FastAPI: el frontend lee ``detail`` y un HTML ahí rompería los
  ``fetch()``.
"""
from __future__ import annotations

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


class TestLanding:
    async def test_renders_without_any_data(self, client):
        r = await client.get("/", headers=HTML)
        assert r.status_code == 200
        body = r.text
        assert 'class="lp-hero"' in body
        # Sin mazos, la mano se rellena con las cinco cartas de ejemplo.
        assert body.count("lp-card--sample") == 5
        assert "landingImport()" in body
        # Sin mazos no se ofrece "seguir donde lo dejaste".
        assert "lp-recent" not in body

    async def test_lists_supported_sites(self, client):
        r = await client.get("/", headers=HTML)
        assert "Moxfield" in r.text
        assert "Archidekt" in r.text

    async def test_shows_recent_decks(self, client, deck):
        r = await client.get("/", headers=HTML)
        assert r.status_code == 200
        assert f'href="/decks/{deck["id"]}"' in r.text
        assert deck["name"] in r.text
        # PDF Studio apunta al mazo más reciente.
        assert f'href="/decks/{deck["id"]}/pdf"' in r.text

    async def test_uses_the_unresolved_modal_partial(self, client):
        r = await client.get("/", headers=HTML)
        assert "unresolvedModal.open" in r.text

    async def test_english_copy(self, client):
        client.cookies.set("lang", "en")
        r = await client.get("/", headers=HTML)
        assert "From decklist to printed sheet." in r.text

    async def test_workshop_status_survives_a_broken_service(self, client, monkeypatch):
        from mpc_forge.services import gdrive_search

        async def boom(db):
            raise RuntimeError("índice a medio migrar")

        monkeypatch.setattr(gdrive_search, "stats", boom)
        r = await client.get("/", headers=HTML)
        assert r.status_code == 200
        assert "lp-status" in r.text


class TestDeckLibraryMoved:
    async def test_library_lives_at_decks(self, client, deck):
        r = await client.get("/decks", headers=HTML)
        assert r.status_code == 200
        assert "importPanel()" in r.text
        assert f'data-deck-cover="{deck["id"]}"' in r.text or deck["name"] in r.text

    async def test_editor_back_link_points_to_library(self, client, deck):
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        assert r.status_code == 200
        assert 'href="/decks" class="text-fg-muted' in r.text

    async def test_nav_marks_decks_active_inside_the_editor(self, client, deck):
        r = await client.get(f"/decks/{deck['id']}", headers=HTML)
        # Un único indicador de sección, y es el de Mazos, no el de Inicio.
        assert r.text.count('class="nav-indicator"') == 1
        nav_decks = r.text.index('href="/decks"\n')
        indicator = r.text.index('class="nav-indicator"')
        assert indicator > nav_decks


class TestNotFoundPage:
    async def test_unknown_page_renders_the_styled_404(self, client):
        r = await client.get("/esto-no-existe", headers=HTML)
        assert r.status_code == 404
        assert "text/html" in r.headers["content-type"]
        assert 'class="nf"' in r.text
        # La ruta aparece en la carta, con <wbr> tras cada "/" para que el
        # navegador pueda partirla por ahí.
        code = r.text.split("<code>", 1)[1].split("</code>", 1)[0]
        assert code.replace("<wbr>", "") == "/esto-no-existe"

    async def test_missing_deck_uses_the_deck_copy(self, client):
        r = await client.get("/decks/999999", headers=HTML)
        assert r.status_code == 404
        assert 'class="nf"' in r.text
        from mpc_forge.services.i18n import get_translations
        assert get_translations("es").nf_deck_body in r.text

    async def test_missing_deck_pdf_and_proof_also_404(self, client):
        for suffix in ("pdf", "proof"):
            r = await client.get(f"/decks/999999/{suffix}", headers=HTML)
            assert r.status_code == 404
            assert 'class="nf"' in r.text

    async def test_api_404_stays_json(self, client):
        r = await client.get("/api/no-such-endpoint", headers=HTML)
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}

    async def test_static_404_stays_plain(self, client):
        r = await client.get("/static/no-such-file.js", headers=HTML)
        assert r.status_code == 404
        assert 'class="nf"' not in r.text

    async def test_non_browser_clients_get_json(self, client):
        r = await client.get("/esto-no-existe")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}

    async def test_non_get_requests_are_untouched(self, client):
        r = await client.post("/esto-no-existe", headers=HTML)
        assert r.status_code in (404, 405)
        assert 'class="nf"' not in r.text

    async def test_long_paths_are_truncated_on_the_card(self, client):
        long_path = "/" + "x" * 120
        r = await client.get(long_path, headers=HTML)
        assert r.status_code == 404
        assert "x" * 120 not in r.text.split("<code>", 1)[1].split("</code>", 1)[0]

    async def test_path_is_escaped(self, client):
        r = await client.get("/%3Cscript%3Ealert(1)%3C%2Fscript%3E", headers=HTML)
        assert r.status_code == 404
        assert "<script>alert(1)" not in r.text


class TestSidebarLayout:
    async def test_sidebar_follows_the_scroll(self, client):
        r = await client.get("/", headers=HTML)
        aside = r.text.split("<aside", 1)[1].split(">", 1)[0]
        for cls in ("sticky", "top-0", "h-screen"):
            assert cls in aside.split('class="', 1)[1].split('"', 1)[0].split()

    async def test_search_results_escape_the_scrolling_nav(self, client):
        """El <nav> hace scroll y recorta a sus descendientes: el desplegable
        del buscador tiene que vivir fuera (teleport a <body>, fixed)."""
        r = await client.get("/", headers=HTML)
        teleport = r.text.index('<template x-teleport="body">')
        dropdown = r.text.index('id="global-search-results"')
        assert teleport < dropdown < r.text.index("</nav>")
        assert "fixed z-[60]" in r.text[dropdown:dropdown + 600]


class TestStatusPlurals:
    async def test_one_deck_is_singular(self, client, deck):
        r = await client.get("/", headers=HTML)
        assert "1 mazo y 0 cartas marcadas en tu colección" in r.text

    async def test_english_plurals(self, client, deck):
        client.cookies.set("lang", "en")
        r = await client.get("/", headers=HTML)
        assert "1 deck and 0 marked cards in your collection" in r.text
