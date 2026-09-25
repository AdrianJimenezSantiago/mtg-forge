"""Portada real de los mazos y búsqueda de artes de drives sin topes ocultos.

Portadas
--------
La portada tiene que ser el arte que el mazo USA para su commander: si el
usuario cambia la impresión o elige un arte custom (de un drive o propio),
todas las vistas que enseñan la portada deben reflejarlo: landing, biblioteca,
cabecera del editor, historial y "Recientes" de la barra lateral.

Búsqueda en drives
------------------
1. El selector pedía `limit=100` y el servidor solo evaluaba 300 candidatos:
   cartas con cientos de artes quedaban recortadas en silencio. Ahora el
   endpoint pagina y publica el total en `X-Total-Count`.
2. Con FTS5, la relevancia se calculaba como `100 + bm25 * 2`. En FTS5 un
   bm25 más negativo es MEJOR, y su magnitud crece con el tamaño del índice y
   la rareza de los términos: las cartas con nombres largos o poco comunes
   (Elesh Norn, Grand Cenobite) acababan por debajo del umbral y la búsqueda
   devolvía CERO artes aunque existieran.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

HTML = {"accept": "text/html"}


@pytest_asyncio.fixture
async def commander_deck(client, sample_cards):
    """Mazo con Sol Ring como commander (impresión `sr-en`)."""
    from mpc_forge.db import session_scope
    from mpc_forge.models import Deck, DeckCard
    from mpc_forge.services import deck_service

    async with session_scope() as db:
        for key in ("Sol Ring", "Sol Ring ALT", "Command Tower"):
            await deck_service.upsert_printing(db, sample_cards[key])
        deck = Deck(name="Mazo Portada", format="commander", commander_scryfall_id="sr-en")
        db.add(deck)
        await db.flush()
        commander = DeckCard(
            deck_id=deck.id, oracle_id="oracle-sol-ring", name="Sol Ring",
            quantity=1, scryfall_id="sr-en", role="commander",
        )
        db.add(commander)
        db.add(DeckCard(
            deck_id=deck.id, oracle_id="oracle-command-tower", name="Command Tower",
            quantity=1, scryfall_id="ct-en", role="mainboard",
        ))
        await db.commit()
        return {"deck_id": deck.id, "commander_id": commander.id}


async def _all_cover_urls(client, deck_id: int) -> dict[str, str]:
    """La portada de `deck_id` tal como la ve cada vista."""
    out = {}
    deck = (await client.get(f"/api/decks/{deck_id}")).json()
    cover_card = next(c for c in deck["cards"] if c["id"] == deck["cover_card_id"])
    out["editor (cover_card_id)"] = cover_card["thumbnail_url"]

    recent = (await client.get("/api/collection/recent-decks")).json()
    out["barra lateral"] = next(d for d in recent if d["id"] == deck_id)["commander_image"]

    activity = (await client.get("/api/decks/_/with-activity")).json()
    out["historial"] = next(d for d in activity if d["id"] == deck_id)["commander_image_url"]

    library = (await client.get("/decks", headers=HTML)).text
    out["biblioteca"] = library.split(f'data-deck-cover="{deck_id}"')[0].rsplit('src="', 1)[1].split('"', 1)[0]

    landing = (await client.get("/", headers=HTML)).text
    out["landing"] = landing.split(f'data-deck-cover="{deck_id}"')[0].rsplit('src="', 1)[1].split('"', 1)[0]

    editor = (await client.get(f"/decks/{deck_id}", headers=HTML)).text
    out["cabecera del editor"] = editor.split('class="fx-deck-cover')[0].rsplit('<img src="', 1)[1].split('"', 1)[0]
    return out


def _assert_everywhere(urls: dict[str, str], expected: str) -> None:
    wrong = {view: url for view, url in urls.items() if url != expected}
    assert not wrong, f"Esperaba {expected} en todas las vistas; distinto en: {wrong}"


class TestDeckCover:
    async def test_default_cover_is_the_imported_printing(self, client, commander_deck):
        urls = await _all_cover_urls(client, commander_deck["deck_id"])
        _assert_everywhere(urls, "https://x.test/sr-en_n.jpg")

    async def test_changed_printing_is_reflected_everywhere(self, client, commander_deck):
        r = await client.post(f"/api/decks/{commander_deck['deck_id']}/cards/change-art", json={
            "deck_card_id": commander_deck["commander_id"], "scryfall_id": "sr-alt", "face": "front",
        })
        assert r.status_code == 200, r.text
        urls = await _all_cover_urls(client, commander_deck["deck_id"])
        _assert_everywhere(urls, "https://x.test/sr-alt_n.jpg")

    async def test_custom_art_is_reflected_everywhere(self, client, commander_deck):
        from mpc_forge.db import session_scope
        from mpc_forge.models import CustomArt
        from mpc_forge.services import custom_art

        async with session_scope() as db:
            ca = CustomArt(
                filename="Sol Ring #02.png", relative_path="sol ring/Sol Ring #02.png",
                card_name_normalized="sol ring", face="front",
            )
            db.add(ca)
            await db.commit()
            ca_id, rel = ca.id, ca.relative_path

        r = await client.post(f"/api/decks/{commander_deck['deck_id']}/cards/change-art", json={
            "deck_card_id": commander_deck["commander_id"], "custom_art_id": ca_id, "face": "front",
        })
        assert r.status_code == 200, r.text
        expected = custom_art.custom_art_url(rel)
        assert "%23" in expected
        urls = await _all_cover_urls(client, commander_deck["deck_id"])
        _assert_everywhere(urls, expected)

    async def test_back_face_custom_art_does_not_change_the_cover(self, client, commander_deck):
        from mpc_forge.db import session_scope
        from mpc_forge.models import CustomArt

        async with session_scope() as db:
            ca = CustomArt(filename="b.png", relative_path="sol ring/b.png",
                           card_name_normalized="sol ring", face="back")
            db.add(ca)
            await db.commit()
            ca_id = ca.id
        r = await client.post(f"/api/decks/{commander_deck['deck_id']}/cards/change-art", json={
            "deck_card_id": commander_deck["commander_id"], "custom_art_id": ca_id, "face": "back",
        })
        assert r.status_code == 200, r.text
        urls = await _all_cover_urls(client, commander_deck["deck_id"])
        _assert_everywhere(urls, "https://x.test/sr-en_n.jpg")

    async def test_deck_without_commander_card_keeps_the_old_fallback(self, client, sample_cards):
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck
        from mpc_forge.services import deck_covers, deck_service

        async with session_scope() as db:
            await deck_service.upsert_printing(db, sample_cards["Sol Ring"])
            deck = Deck(name="Antiguo", format="commander", commander_scryfall_id="sr-en")
            db.add(deck)
            await db.commit()
            cover = await deck_covers.cover_for_deck(db, deck)
        assert cover.image_url == "https://x.test/sr-en_n.jpg"
        assert cover.name == "Sol Ring"
        assert cover.card_id is None

    async def test_deck_without_any_commander(self, client, deck):
        from mpc_forge.db import session_scope
        from mpc_forge.models import Deck
        from mpc_forge.services import deck_covers

        async with session_scope() as db:
            d = await db.get(Deck, deck["id"])
            cover = await deck_covers.cover_for_deck(db, d)
        assert cover == deck_covers.EMPTY
        r = await client.get(f"/api/decks/{deck['id']}")
        assert r.json()["cover_card_id"] is None


class TestPickCoverCard:
    """Con compañeros (partner/background) hay dos cartas `commander`."""

    @staticmethod
    def _card(id_, sfid, oracle):
        from mpc_forge.models import DeckCard
        return DeckCard(id=id_, deck_id=1, oracle_id=oracle, name=oracle, quantity=1,
                        scryfall_id=sfid, role="commander")

    @staticmethod
    def _printing(sfid, oracle):
        from mpc_forge.models import PrintingCache
        return PrintingCache(scryfall_id=sfid, oracle_id=oracle, name=oracle, set_code="x",
                             set_name="X", collector_number="1", rarity="rare")

    def test_prefers_the_card_with_the_imported_printing(self):
        from mpc_forge.models import Deck
        from mpc_forge.services.deck_covers import pick_cover_card
        a, b = self._card(1, "a1", "oa"), self._card(2, "b1", "ob")
        deck = Deck(name="x", format="commander", commander_scryfall_id="b1")
        assert pick_cover_card(deck, [a, b], {}) is b

    def test_follows_the_commander_after_its_art_changed(self):
        from mpc_forge.models import Deck
        from mpc_forge.services.deck_covers import pick_cover_card
        a, b = self._card(1, "a1", "oa"), self._card(2, "b2", "ob")
        deck = Deck(name="x", format="commander", commander_scryfall_id="b1")
        printings = {"b1": self._printing("b1", "ob")}
        assert pick_cover_card(deck, [a, b], printings) is b

    def test_falls_back_to_the_first_commander(self):
        from mpc_forge.models import Deck
        from mpc_forge.services.deck_covers import pick_cover_card
        a, b = self._card(1, "a1", "oa"), self._card(2, "b1", "ob")
        deck = Deck(name="x", format="commander", commander_scryfall_id=None)
        assert pick_cover_card(deck, [a, b], {}) is a
        assert pick_cover_card(deck, [], {}) is None


class TestDeckCoverBatching:
    async def test_constant_number_of_queries(self, client, sample_cards):
        """La biblioteca pide portadas de todos los mazos: nada de N+1."""
        from sqlalchemy import event

        from mpc_forge.db import engine, session_scope
        from mpc_forge.models import Deck, DeckCard
        from mpc_forge.services import deck_covers, deck_service

        async with session_scope() as db:
            await deck_service.upsert_printing(db, sample_cards["Sol Ring"])
            decks = []
            for i in range(25):
                d = Deck(name=f"D{i}", format="commander", commander_scryfall_id="sr-en")
                db.add(d)
                await db.flush()
                db.add(DeckCard(deck_id=d.id, oracle_id="oracle-sol-ring", name="Sol Ring",
                                quantity=1, scryfall_id="sr-en", role="commander"))
                decks.append(d)
            await db.commit()

            statements = []
            listener = lambda *a, **k: statements.append(a[2])  # noqa: E731
            event.listen(engine.sync_engine, "before_cursor_execute", listener)
            try:
                covers = await deck_covers.covers_for_decks(db, decks)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", listener)
        assert len(covers) == 25
        assert all(c.image_url == "https://x.test/sr-en_n.jpg" for c in covers.values())
        assert len(statements) <= 3, statements


async def _seed_index(n_sol_ring=0, rare=(), filler=0):
    """Inserta artes directamente en el índice (los triggers mantienen FTS5)."""
    import random

    from mpc_forge.db import session_scope
    from mpc_forge.models import ArtSource, IndexedArt
    from mpc_forge.services.gdrive_search import normalize_filename

    rnd = random.Random(5)
    words = [f"w{i}" for i in range(600)]
    async with session_scope() as db:
        src = ArtSource(name="Drive Test", url="https://drive.google.com/drive/folders/test")
        db.add(src)
        await db.flush()
        rows = []

        def add(filename):
            rows.append(IndexedArt(
                source_id=src.id, file_id=f"f{len(rows)}", filename=filename,
                name_normalized=normalize_filename(filename), folder_path="", tags="",
            ))

        for k in range(n_sol_ring):
            add(f"Sol Ring (Artist {k:03d}).png")
        for name, count in rare:
            for k in range(count):
                add(f"{name} (v{k}).png")
        for k in range(filler):
            add(" ".join(rnd.sample(words, 2)) + f" {k}.png")
        db.add_all(rows)
        await db.commit()


@pytest.fixture
def fts5_on():
    """Garantiza que la búsqueda usa FTS5 (y restaura la caché al acabar)."""
    from mpc_forge.services import gdrive_search
    before = gdrive_search._fts5_available_cache
    gdrive_search._fts5_available_cache = None
    yield
    gdrive_search._fts5_available_cache = before


@pytest.fixture
def fts5_off():
    from mpc_forge.services import gdrive_search
    before = gdrive_search._fts5_available_cache
    gdrive_search._fts5_available_cache = False
    yield
    gdrive_search._fts5_available_cache = before


class TestDriveSearchPagination:
    async def test_more_than_the_old_caps_are_reachable(self, client, fts5_on):
        await _seed_index(n_sol_ring=450)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert r.status_code == 200
        assert r.headers["X-Total-Count"] == "450"
        assert "X-Total-Capped" not in r.headers
        assert len(r.json()) == 450

    async def test_pages_cover_everything_without_overlap(self, client, fts5_on):
        await _seed_index(n_sol_ring=450)
        seen = []
        offset = 0
        while True:
            r = await client.get("/api/drives/search",
                                 params={"q": "Sol Ring", "limit": 100, "offset": offset})
            page = r.json()
            assert r.headers["X-Total-Count"] == "450"
            seen += [h["file_id"] for h in page]
            if len(page) < 100:
                break
            offset += 100
        assert len(seen) == 450
        assert len(set(seen)) == 450

    async def test_order_is_stable_between_calls(self, client, fts5_on):
        await _seed_index(n_sol_ring=120)
        a = (await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 50, "offset": 50})).json()
        b = (await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 50, "offset": 50})).json()
        assert [h["file_id"] for h in a] == [h["file_id"] for h in b]

    async def test_like_fallback_paginates_too(self, client, fts5_off):
        await _seed_index(n_sol_ring=420)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert r.headers["X-Total-Count"] == "420"
        assert len(r.json()) == 420

    async def test_limit_is_bounded(self, client):
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 501})
        assert r.status_code == 422
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "offset": -1})
        assert r.status_code == 422

    async def test_empty_query_reports_zero(self, client):
        r = await client.get("/api/drives/search", params={"q": "  "})
        assert r.json() == []
        assert r.headers["X-Total-Count"] == "0"

    async def test_cap_is_reported(self, client, fts5_on, monkeypatch):
        from mpc_forge.services import gdrive_search
        monkeypatch.setattr(gdrive_search, "_MAX_CANDIDATES", 50)
        await _seed_index(n_sol_ring=80)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert r.headers["X-Total-Capped"] == "1"
        assert r.headers["X-Total-Count"] == "50"


class TestDriveSearchRelevance:
    RARE = (("Elesh Norn, Grand Cenobite", 6), ("Atraxa, Praetors' Voice", 4), ("Thassa's Oracle", 3))

    @pytest.mark.parametrize("name,count", RARE)
    async def test_rare_long_names_are_found(self, client, fts5_on, name, count):
        """Con FTS5 y un índice de miles de artes, estas búsquedas devolvían 0."""
        await _seed_index(n_sol_ring=50, rare=self.RARE, filler=3000)
        from mpc_forge.db import session_scope
        from mpc_forge.services import gdrive_search

        async with session_scope() as db:
            assert await gdrive_search._fts5_available(db), "este test necesita FTS5"
            results = await gdrive_search.search(db, name, limit=100)
        assert len(results) == count
        assert all(r.score == 100 for r in results)

    async def test_fts_and_like_agree(self, client, fts5_on):
        await _seed_index(n_sol_ring=30, rare=self.RARE, filler=500)
        from mpc_forge.db import session_scope
        from mpc_forge.services import gdrive_search

        async with session_scope() as db:
            fts = await gdrive_search.search_page(db, "Elesh Norn, Grand Cenobite", limit=100)
            gdrive_search._fts5_available_cache = False
            like = await gdrive_search.search_page(db, "Elesh Norn, Grand Cenobite", limit=100)
        assert fts.total == like.total == 6
        assert {r.file_id for r in fts.results} == {r.file_id for r in like.results}

    async def test_other_cards_stay_out(self, client, fts5_on):
        from mpc_forge.db import session_scope
        from mpc_forge.models import ArtSource, IndexedArt
        from mpc_forge.services import gdrive_search
        from mpc_forge.services.gdrive_search import normalize_filename

        async with session_scope() as db:
            src = ArtSource(name="D", url="https://drive.google.com/drive/folders/d")
            db.add(src)
            await db.flush()
            for i, fn in enumerate(["Forest.png", "Forest (Full Art).png", "Forest Warden.png",
                                    "Sol Ring.png", "Cursed Sol Ring.png"]):
                db.add(IndexedArt(source_id=src.id, file_id=f"x{i}", filename=fn,
                                  name_normalized=normalize_filename(fn), folder_path="", tags=""))
            await db.commit()
            forest = await gdrive_search.search(db, "Forest", limit=100)
            sol = await gdrive_search.search(db, "Sol Ring", limit=100)
        assert sorted(r.filename for r in forest) == ["Forest (Full Art).png", "Forest.png"]
        assert sol[0].filename == "Sol Ring.png"
        assert all(r.score < 100 for r in sol[1:])

    async def test_empty_fts_result_falls_back_to_like(self, client, fts5_on):
        """Erratas: FTS5 no las encuentra; el modo LIKE sí (rapidfuzz)."""
        await _seed_index(n_sol_ring=5)
        from mpc_forge.db import session_scope
        from mpc_forge.services import gdrive_search

        async with session_scope() as db:
            results = await gdrive_search.search(db, "Sol Rnig", limit=10)
        assert results, "una errata debería seguir encontrando Sol Ring"
