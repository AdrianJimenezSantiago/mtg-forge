from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from sqlalchemy import select

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import OracleArtistCache
from mpc_forge.services.art import art_library, phash, thumbnails
from mpc_forge.services.art.recommender import _fold, _pick_best_printing

needs_pillow = pytest.mark.skipif(
    not thumbnails.pillow_available(), reason="Pillow no está instalado"
)


def _prints_url(deck: dict, **params) -> str:
    card = deck["cards"][0]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"/api/decks/{deck['id']}/cards/{card['id']}/prints" + (f"?{query}" if query else "")


def _image(path: Path, size=(745, 1040), color=(30, 60, 120)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


class TestArtPicker:
    async def test_prints_are_a_paginated_envelope(self, client, deck):
        body = (await client.get(_prints_url(deck))).json()
        assert {"items", "custom", "total", "offset", "limit", "has_more", "facets"} <= set(body)
        assert body["limit"] == 60 and len(body["items"]) <= 60
        assert {"total", "custom", "full_art", "textless", "promo", "borderless", "retro"} <= set(
            body["facets"]
        )

        first = (await client.get(_prints_url(deck, limit=1))).json()
        second = (await client.get(_prints_url(deck, limit=1, offset=1))).json()
        assert len(first["items"]) <= 1 and first["limit"] == 1
        assert first["has_more"] == (first["total"] > 1)
        assert first["total"] == second["total"] and second["custom"] == []
        if first["total"] > 1:
            assert first["items"][0] != second["items"][0]

    async def test_filters_sorts_and_validation(self, client, deck):
        base = (await client.get(_prints_url(deck))).json()
        filtered = (await client.get(_prints_url(deck, only="full_art"))).json()
        assert filtered["total"] <= base["total"]
        assert filtered["facets"]["total"] == base["facets"]["total"]
        assert filtered["facets"]["textless"] == base["facets"]["textless"]
        assert (await client.get(_prints_url(deck, q="zzzznoexiste"))).json()["total"] == 0

        for sort in ("released_desc", "released_asc", "set", "rarity", "artist"):
            assert (await client.get(_prints_url(deck, sort=sort))).status_code == 200, sort
        assert (await client.get(_prints_url(deck, sort="drop_table"))).status_code == 400
        assert (await client.get(_prints_url(deck, only="inventada"))).status_code == 400
        assert (await client.get(_prints_url(deck, offset=-1))).status_code == 422
        assert (await client.get(_prints_url(deck, limit=5000))).status_code == 422
        card_id = deck["cards"][0]["id"]
        assert (await client.get(f"/api/decks/999999/cards/{card_id}/prints")).status_code == 404


class TestThumbnails:
    def test_paths_and_urls(self, tmp_path, monkeypatch):
        target = thumbnails.thumb_path_for(cfg.PATHS.art_dir / "ab" / "cd1234.png")
        assert (
            target.suffix == ".webp"
            and target.is_relative_to(cfg.PATHS.thumbs_dir)
            and "ab" in target.parts
        )
        assert thumbnails.url_for_relative("ab\\cd1234.png") == "/api/thumb/ab/cd1234.png"
        assert thumbnails.url_for_relative("/ab/cd.png") == "/api/thumb/ab/cd.png"

        monkeypatch.setattr(cfg, "PATHS", cfg.PATHS.with_overrides(art_dir=str(tmp_path / "arte")))
        a = thumbnails.thumb_path_for(tmp_path / "drive_a" / "Sol Ring.png")
        b = thumbnails.thumb_path_for(tmp_path / "drive_b" / "Sol Ring.png")
        assert "_external" in a.parts and "_external" in b.parts and a != b
        assert a.is_relative_to(cfg.PATHS.thumbs_dir)

    def test_same_folder_through_a_symlink_gives_the_same_thumb(self, tmp_path, monkeypatch):
        real, alias = tmp_path / "real_art", tmp_path / "alias_art"
        real.mkdir()
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("El sistema no permite crear enlaces simbólicos")
        monkeypatch.setattr(cfg, "PATHS", cfg.PATHS.with_overrides(art_dir=str(alias)))
        via_alias = thumbnails.thumb_path_for(alias / "sub" / "Sol Ring.png")
        via_real = thumbnails.thumb_path_for(real / "sub" / "Sol Ring.png")
        assert "_external" not in via_alias.parts and "_external" not in via_real.parts
        assert via_alias == via_real

    @needs_pillow
    async def test_generation(self, tmp_path):
        weird = tmp_path / "art.tiff.xyz"
        weird.write_bytes(b"whatever")
        assert await thumbnails.ensure_thumb(cfg.PATHS.art_dir / "nope.png") is None
        assert await thumbnails.ensure_thumb(weird) is None
        corrupt = cfg.PATHS.art_dir / "corrupt.png"
        corrupt.write_bytes(b"esto no es un PNG")
        assert await thumbnails.ensure_thumb(corrupt) is None

        from PIL import Image

        source = _image(cfg.PATHS.art_dir / "thumbtest.png")
        thumb = await thumbnails.ensure_thumb(source)
        assert thumb.suffix == ".webp" and thumb.stat().st_size < source.stat().st_size
        with Image.open(thumb) as im:
            assert im.width <= thumbnails.THUMB_WIDTH and im.height <= thumbnails.THUMB_HEIGHT
        stamp = thumb.stat().st_mtime_ns
        assert await thumbnails.ensure_thumb(source) == thumb and thumb.stat().st_mtime_ns == stamp

    @needs_pillow
    async def test_concurrency_and_decompression_bombs(self, tmp_path, monkeypatch):
        from PIL import Image

        source = _image(tmp_path / "carta.png", (600, 840), "red")
        calls = 0
        original = thumbnails._generate_sync

        def counting(src, dst):
            nonlocal calls
            calls += 1
            return original(src, dst)

        monkeypatch.setattr(thumbnails, "_generate_sync", counting)
        results = await asyncio.gather(*(thumbnails.ensure_thumb(source) for _ in range(40)))
        assert all(r is not None for r in results) and calls == 1

        before = Image.MAX_IMAGE_PIXELS
        monkeypatch.setattr(thumbnails, "MAX_SOURCE_PIXELS", 1000)
        assert (
            await thumbnails.ensure_thumb(_image(tmp_path / "bomba.png", (200, 200), "blue"))
            is None
        )
        assert before == Image.MAX_IMAGE_PIXELS

    async def test_endpoints(self, client):
        assert (await client.get("/api/thumb/no/existe.png")).status_code == 404
        for attack in (
            "../../../etc/passwd",
            "..%2f..%2f..%2fetc%2fpasswd",
            "a/../../../../etc/passwd",
        ):
            r = await client.get(f"/api/thumb/{attack}")
            assert r.status_code in (400, 404) and b"root:" not in r.content, attack
        assert {"count", "available"} <= set((await client.get("/api/thumbs/stats")).json())
        assert "removed" in (await client.post("/api/thumbs/clear")).json()

    @needs_pillow
    async def test_endpoint_generates_on_demand_and_caches(self, client):
        source = _image(cfg.PATHS.art_dir / "ondemand.png", color=(200, 50, 50))
        r = await client.get("/api/thumb/ondemand.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        assert "immutable" in r.headers.get("cache-control", "")
        assert thumbnails.thumb_path_for(source).exists()
        assert (await client.get("/api/thumbs/stats")).json()["count"] >= 1


class _IndexedRow:
    id = 1
    file_id = "FILE123"
    filename = "Sol Ring.png"
    source_id = 1
    folder_path = "carpeta"
    size_bytes = 1024
    tags = "full_art,retro"
    expansion_code = "dmu"
    collector_number = "100"
    card_type = "CARD"
    image_hash = None
    thumb_url = None
    download_url = None
    indexed_at = None
    is_full_art = True
    is_borderless = False
    is_extended = False
    is_showcase = False
    is_retro = True
    is_textless = False
    is_promo = False
    is_alt_art = False


class TestArtLibrary:
    def test_filters_sorts_and_variants(self):
        assert art_library.LibraryFilters().is_empty()
        for field, value in (
            ("query", "sol ring"),
            ("expansion_code", "dmu"),
            ("card_type", "TOKEN"),
            ("source_ids", [1]),
            ("variants", ["full_art"]),
        ):
            assert not art_library.LibraryFilters(**{field: value}).is_empty(), field
        assert art_library.DEFAULT_SORT in art_library.SORT_OPTIONS
        unstable = [
            n for n, clauses in art_library.SORT_OPTIONS.items() if "id" not in str(clauses[-1])
        ]
        assert not unstable, f"Órdenes sin desempate por id: {unstable}"
        assert set(art_library.VARIANT_FLAGS) == {
            "full_art",
            "borderless",
            "extended",
            "showcase",
            "retro",
            "textless",
            "promo",
            "alt_art",
        }
        assert art_library.DEFAULT_PAGE_SIZE <= art_library.MAX_PAGE_SIZE <= 500

    def test_serialization(self):
        def serialized(**overrides):
            row = _IndexedRow()
            for key, value in overrides.items():
                setattr(row, key, value)
            return art_library._serialize(row, {1: "Mi Drive"})

        data = serialized()
        assert "FILE123" in data["thumb_url"] and "FILE123" in data["download_url"]
        assert set(data["variants"]) == {"full_art", "retro"} and data["source_name"] == "Mi Drive"
        assert serialized(thumb_url="https://ejemplo/m.png")["thumb_url"] == "https://ejemplo/m.png"
        assert serialized(tags="")["tags"] == [] and serialized(tags="a,,b")["tags"] == ["a", "b"]
        assert serialized(source_id=999)["source_name"] == ""

    async def test_endpoints(self, client):
        overview = (await client.get("/api/library/overview")).json()
        assert (overview["total_arts"], overview["is_empty"]) == (0, True)

        browse = (await client.get("/api/library/browse")).json()
        assert {"items", "total", "offset", "limit", "has_more", "sort"} <= set(browse)
        assert all(item["thumb_url"] for item in browse["items"])
        joined = ",".join(art_library.VARIANT_FLAGS)
        assert (await client.get(f"/api/library/browse?variants={joined}")).status_code == 200
        assert (await client.get("/api/library/browse?sources=abc,1")).status_code == 200
        assert (await client.get("/api/library/browse?sort=drop_table")).status_code == 400
        assert (await client.get("/api/library/browse?variants=inventada")).status_code == 400
        assert (await client.get("/api/library/browse?limit=99999")).status_code == 422

        facets = (await client.get("/api/library/facets")).json()
        assert {"by_source", "by_variant", "by_expansion", "by_card_type"} <= set(facets)
        assert set(facets["by_variant"]) == set(art_library.VARIANT_FLAGS)
        assert "cards" in (await client.get("/api/library/cards")).json()
        variants = (await client.get("/api/library/variants")).json()
        assert set(variants["variants"]) == set(art_library.VARIANT_FLAGS)
        assert set(variants["sorts"]) == set(art_library.SORT_OPTIONS)


def test_phash():
    assert isinstance(phash.is_available(), bool)
    assert phash.hamming_distance("ffffffffffffffff", "ffffffffffffffff") == 0
    assert phash.hamming_distance("ffffffffffffffff", "fffffffffffffffe") == 1
    assert phash.hamming_distance("", "abc") == phash.hamming_distance("bad", "worse") == -1
    if not phash.is_available():
        return
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color=(255, 128, 0)).save(buf, format="PNG")
    value = phash.compute_from_bytes(buf.getvalue())
    assert len(value) == 16 and all(c in "0123456789abcdef" for c in value)


class TestRecommender:
    def test_helpers(self):
        assert _fold("Yeong-Hao Han") == _fold("Yeong Hao Han") == "yeong hao han"
        assert _fold("Rebecca Guay") == "rebecca guay"
        best = _pick_best_printing(
            [
                {"promo": True, "full_art": False, "released_at": "2024-01-01"},
                {"promo": False, "full_art": True, "released_at": "2020-01-01"},
                {"promo": False, "full_art": False, "released_at": "2018-01-01"},
                {"promo": False, "full_art": False, "released_at": "2023-01-01"},
            ]
        )
        assert best["released_at"] == "2023-01-01"

    async def test_endpoints(self, client, deck):
        data = (
            await client.post(
                f"/api/decks/{deck['id']}/recommend-by-artist", json={"artist": "Test Artist"}
            )
        ).json()
        assert data["artist_query"] == "Test Artist" and isinstance(data["matched"], list)
        assert isinstance(data["unmatched_count"], int) and data["total_deck_uniques"] >= 1
        async with session_scope() as s:
            assert (await s.scalars(select(OracleArtistCache))).all()

        url = f"/api/decks/{deck['id']}/recommend-by-style"
        assert (await client.post(url, json={})).json()["matched"] == []
        styled = (await client.post(url, json={"set_code": "c21", "borderless": False})).json()
        assert "matched" in styled and styled["query"]["set_code"] == "c21"
