from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import Deck, DeckCard, PrintingCache
from mpc_forge.services.art import thumbnails
from mpc_forge.services.decks import deck_validation
from mpc_forge.utils.iterables import chunked


class TestTimezoneAwareColumns:
    async def test_datetimes_come_back_aware(self, client):
        async with session_scope() as db:
            deck = Deck(name="tz-test", format="commander")
            db.add(deck)
            await db.flush()
            deck_id = deck.id

        async with session_scope() as db:
            stored = await db.get(Deck, deck_id)
            assert stored.imported_at.tzinfo is not None
            assert stored.updated_at.tzinfo is not None
            assert stored.imported_at <= datetime.now(UTC)

    async def test_roundtrip_preserves_the_instant(self, client):
        madrid = timezone(timedelta(hours=2))
        moment = datetime(2026, 6, 1, 14, 30, tzinfo=madrid)

        async with session_scope() as db:
            row = PrintingCache(
                scryfall_id="tz-1",
                oracle_id="o-1",
                name="Tz Card",
                set_code="tst",
                set_name="Test",
                collector_number="1",
                rarity="common",
                fetched_at=moment,
            )
            db.add(row)

        async with session_scope() as db:
            stored = await db.get(PrintingCache, "tz-1")
            assert stored.fetched_at == moment
            assert stored.fetched_at.hour == 12
            assert stored.fetched_at.tzinfo is not None


class TestInClauseChunking:
    def test_chunks_respect_the_limit(self):
        items = list(range(1201))
        chunks = list(chunked(items, 500))
        assert [len(c) for c in chunks] == [500, 500, 201]
        assert [i for c in chunks for i in c] == items

    def test_empty_input_yields_nothing(self):
        assert list(chunked([], 500)) == []

    async def test_view_survives_a_deck_larger_than_the_sqlite_limit(self, client):
        async with session_scope() as db:
            deck = Deck(name="cubo", format="commander")
            db.add(deck)
            await db.flush()
            for i in range(1100):
                db.add(
                    DeckCard(
                        deck_id=deck.id,
                        scryfall_id=f"sf-{i}",
                        oracle_id=f"or-{i}",
                        name=f"Carta {i}",
                        quantity=1,
                        role="mainboard",
                        include=True,
                    )
                )
            deck_id = deck.id

        r = await client.get(f"/api/decks/{deck_id}")
        assert r.status_code == 200
        assert len(r.json()["cards"]) == 1100


class TestThumbnailGeneration:
    async def test_concurrent_requests_generate_once(self, tmp_path, monkeypatch):
        pytest.importorskip("PIL")
        from PIL import Image

        source = tmp_path / "carta.png"
        Image.new("RGB", (600, 840), "red").save(source)

        calls = 0
        original = thumbnails._generate_sync

        def counting(src, dst):
            nonlocal calls
            calls += 1
            return original(src, dst)

        monkeypatch.setattr(thumbnails, "_generate_sync", counting)

        results = await asyncio.gather(*(thumbnails.ensure_thumb(source) for _ in range(40)))
        assert all(r is not None for r in results)
        assert calls == 1, f"Se generó {calls} veces en lugar de una"

    async def test_decompression_bomb_is_rejected(self, tmp_path, monkeypatch):
        pytest.importorskip("PIL")
        from PIL import Image

        monkeypatch.setattr(thumbnails, "MAX_SOURCE_PIXELS", 1000)

        source = tmp_path / "bomba.png"
        Image.new("RGB", (200, 200), "blue").save(source)

        assert await thumbnails.ensure_thumb(source) is None

    async def test_size_guard_does_not_leak_to_other_code(self, tmp_path, monkeypatch):
        pytest.importorskip("PIL")
        from PIL import Image

        before = Image.MAX_IMAGE_PIXELS
        monkeypatch.setattr(thumbnails, "MAX_SOURCE_PIXELS", 1000)

        source = tmp_path / "grande.png"
        Image.new("RGB", (200, 200), "green").save(source)
        await thumbnails.ensure_thumb(source)

        assert before == Image.MAX_IMAGE_PIXELS, (
            "La generación de miniaturas modificó el límite global de Pillow"
        )


class TestLegalityChecking:
    def _legal(self, **fmts):
        return json.dumps(fmts)

    def test_banned_card_is_reported(self):
        illegal = deck_validation.check_legalities(
            "modern",
            [
                ("Sol Ring", "mainboard", self._legal(modern="banned"), True),
                ("Lightning Bolt", "mainboard", self._legal(modern="legal"), True),
            ],
        )
        assert [c.name for c in illegal] == ["Sol Ring"]
        assert illegal[0].status == "banned"

    def test_maybeboard_is_ignored(self):
        assert (
            deck_validation.check_legalities(
                "modern",
                [
                    ("Sol Ring", "maybeboard", self._legal(modern="banned"), True),
                ],
            )
            == []
        )

    def test_excluded_cards_are_ignored(self):
        assert (
            deck_validation.check_legalities(
                "modern",
                [
                    ("Sol Ring", "mainboard", self._legal(modern="banned"), False),
                ],
            )
            == []
        )

    def test_missing_data_never_produces_a_false_positive(self):
        assert (
            deck_validation.check_legalities(
                "modern",
                [
                    ("Carta Nueva", "mainboard", "", True),
                ],
            )
            == []
        )

    def test_corrupt_json_does_not_break_the_view(self):
        assert (
            deck_validation.check_legalities(
                "modern",
                [
                    ("Rota", "mainboard", "{no es json", True),
                ],
            )
            == []
        )

    def test_banned_card_makes_the_deck_invalid(self):
        illegal = deck_validation.check_legalities(
            "modern",
            [
                ("Sol Ring", "mainboard", self._legal(modern="banned"), True),
            ],
        )
        result = deck_validation.validate_deck("modern", [("mainboard", 60, True)], illegal)
        assert result.is_valid is False
        assert result.level == "error"
        assert "no legal" in result.message

    def test_restricted_is_a_warning_not_an_error(self):
        illegal = deck_validation.check_legalities(
            "vintage",
            [
                ("Black Lotus", "mainboard", self._legal(vintage="restricted"), True),
            ],
        )
        result = deck_validation.validate_deck("vintage", [("mainboard", 60, True)], illegal)
        assert result.level == "warn"
        assert result.is_valid is True

    def test_a_correct_deck_stays_clean(self):
        result = deck_validation.validate_deck("modern", [("mainboard", 60, True)], [])
        assert result.is_valid is True
        assert result.level == "ok"
        assert result.illegal == []


class TestDeckPricing:
    async def test_price_sums_only_playable_roles(self, client):
        async with session_scope() as db:
            db.add(
                PrintingCache(
                    scryfall_id="p-1",
                    oracle_id="o-1",
                    name="Cara",
                    set_code="tst",
                    set_name="Test",
                    collector_number="1",
                    rarity="rare",
                    price_eur=10.0,
                    price_usd=12.0,
                )
            )
            db.add(
                PrintingCache(
                    scryfall_id="p-2",
                    oracle_id="o-2",
                    name="Idea",
                    set_code="tst",
                    set_name="Test",
                    collector_number="2",
                    rarity="rare",
                    price_eur=99.0,
                    price_usd=99.0,
                )
            )
            deck = Deck(name="precios", format="commander")
            db.add(deck)
            await db.flush()
            db.add(
                DeckCard(
                    deck_id=deck.id,
                    scryfall_id="p-1",
                    oracle_id="o-1",
                    name="Cara",
                    quantity=2,
                    role="mainboard",
                    include=True,
                )
            )
            db.add(
                DeckCard(
                    deck_id=deck.id,
                    scryfall_id="p-2",
                    oracle_id="o-2",
                    name="Idea",
                    quantity=1,
                    role="maybeboard",
                    include=True,
                )
            )
            deck_id = deck.id

        price = (await client.get(f"/api/decks/{deck_id}")).json()["price"]
        assert price["eur"] == 20.0
        assert price["usd"] == 24.0
        assert price["priced_cards"] == 2

    async def test_cards_without_price_are_counted_apart(self, client):
        async with session_scope() as db:
            db.add(
                PrintingCache(
                    scryfall_id="p-3",
                    oracle_id="o-3",
                    name="Sin precio",
                    set_code="tst",
                    set_name="Test",
                    collector_number="3",
                    rarity="rare",
                    price_eur=None,
                    price_usd=None,
                )
            )
            deck = Deck(name="sin-precio", format="commander")
            db.add(deck)
            await db.flush()
            db.add(
                DeckCard(
                    deck_id=deck.id,
                    scryfall_id="p-3",
                    oracle_id="o-3",
                    name="Sin precio",
                    quantity=3,
                    role="mainboard",
                    include=True,
                )
            )
            deck_id = deck.id

        price = (await client.get(f"/api/decks/{deck_id}")).json()["price"]
        assert price["eur"] == 0.0
        assert price["unpriced_cards"] == 3
        assert price["priced_cards"] == 0


class TestThumbPathNormalization:
    def test_same_dir_written_two_ways_gives_the_same_thumb(self, tmp_path, monkeypatch):
        real = tmp_path / "real_art"
        real.mkdir()
        alias = tmp_path / "alias_art"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("El sistema no permite crear enlaces simbólicos")

        paths_via_alias = cfg.PATHS.with_overrides(art_dir=str(alias))
        monkeypatch.setattr(cfg, "PATHS", paths_via_alias)

        via_alias = thumbnails.thumb_path_for(alias / "sub" / "Sol Ring.png")
        via_real = thumbnails.thumb_path_for(real / "sub" / "Sol Ring.png")

        assert "_external" not in via_alias.parts
        assert "_external" not in via_real.parts, (
            "La ruta real no se reconoció como interna a art_dir: volvería a "
            "caer al cajón _external, que es el bug de Windows."
        )
        assert via_alias == via_real

    def test_external_arts_with_the_same_name_do_not_collide(self, tmp_path, monkeypatch):
        paths = cfg.PATHS.with_overrides(art_dir=str(tmp_path / "arte"))
        monkeypatch.setattr(cfg, "PATHS", paths)

        a = thumbnails.thumb_path_for(tmp_path / "drive_a" / "Sol Ring.png")
        b = thumbnails.thumb_path_for(tmp_path / "drive_b" / "Sol Ring.png")

        assert "_external" in a.parts and "_external" in b.parts
        assert a != b, "Dos artes distintos comparten fichero de miniatura"
