"""Tests de la paginación del selector de arte y del servicio de miniaturas.

Cubren el ítem 7 (miniaturas WebP locales) y el 8 (paginación real en
``/prints``) de la hoja de ruta.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mpc_forge.config import PATHS
from mpc_forge.services import thumbnails


def _prints_url(deck, card, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = f"/api/decks/{deck['id']}/cards/{card['id']}/prints"
    return f"{base}?{query}" if query else base


@pytest.fixture
def any_card(deck):
    return deck["cards"][0]


class TestPrintsEnvelope:
    async def test_returns_paginated_envelope(self, client, deck, any_card):
        r = await client.get(_prints_url(deck, any_card))
        assert r.status_code == 200
        body = r.json()
        for key in ("items", "custom", "total", "offset", "limit", "has_more", "facets"):
            assert key in body, f"Falta '{key}' en la respuesta paginada"

    async def test_default_limit_is_applied(self, client, deck, any_card):
        body = (await client.get(_prints_url(deck, any_card))).json()
        assert body["limit"] == 60
        assert len(body["items"]) <= 60

    async def test_limit_is_respected(self, client, deck, any_card):
        body = (await client.get(_prints_url(deck, any_card, limit=1))).json()
        assert len(body["items"]) <= 1
        assert body["limit"] == 1

    async def test_has_more_is_consistent_with_total(self, client, deck, any_card):
        body = (await client.get(_prints_url(deck, any_card, limit=1))).json()
        assert body["has_more"] == (body["total"] > 1)

    async def test_offset_skips_items(self, client, deck, any_card):
        first = (await client.get(_prints_url(deck, any_card, limit=1))).json()
        if first["total"] < 2:
            pytest.skip("La carta de prueba solo tiene una impresión")
        second = (
            await client.get(_prints_url(deck, any_card, limit=1, offset=1))
        ).json()
        assert first["items"][0] != second["items"][0], (
            "offset=1 debe devolver un elemento distinto al de offset=0"
        )

    async def test_customs_only_on_first_page(self, client, deck, any_card):
        """Repetir los customs en cada página los duplicaría en la rejilla."""
        page_two = (
            await client.get(_prints_url(deck, any_card, limit=1, offset=1))
        ).json()
        assert page_two["custom"] == []

    async def test_total_is_stable_across_pages(self, client, deck, any_card):
        a = (await client.get(_prints_url(deck, any_card, limit=1))).json()
        b = (await client.get(_prints_url(deck, any_card, limit=1, offset=1))).json()
        assert a["total"] == b["total"]


class TestPrintsValidation:
    async def test_unknown_sort_is_rejected(self, client, deck, any_card):
        r = await client.get(_prints_url(deck, any_card, sort="drop_table"))
        assert r.status_code == 400

    async def test_unknown_facet_is_rejected(self, client, deck, any_card):
        r = await client.get(_prints_url(deck, any_card, only="inventada"))
        assert r.status_code == 400

    async def test_negative_offset_is_rejected(self, client, deck, any_card):
        r = await client.get(_prints_url(deck, any_card, offset=-1))
        assert r.status_code == 422

    async def test_excessive_limit_is_rejected(self, client, deck, any_card):
        """Sin tope, `limit=999999` reintroduce el problema que resolvimos."""
        r = await client.get(_prints_url(deck, any_card, limit=5000))
        assert r.status_code == 422

    @pytest.mark.parametrize(
        "sort",
        ["released_desc", "released_asc", "set", "rarity", "artist"],
    )
    async def test_every_documented_sort_works(self, client, deck, any_card, sort):
        r = await client.get(_prints_url(deck, any_card, sort=sort))
        assert r.status_code == 200

    async def test_card_from_another_deck_is_404(self, client, deck, any_card):
        r = await client.get(f"/api/decks/999999/cards/{any_card['id']}/prints")
        assert r.status_code == 404


class TestPrintsFacets:
    async def test_facets_include_every_filter(self, client, deck, any_card):
        facets = (await client.get(_prints_url(deck, any_card))).json()["facets"]
        for key in ("total", "custom", "full_art", "textless",
                    "promo", "borderless", "retro"):
            assert key in facets

    async def test_facets_do_not_shrink_when_filtering(self, client, deck, any_card):
        """El contador debe describir el conjunto entero, no el ya filtrado.

        Si al marcar "full art" el contador de "textless" bajara, el usuario no
        podría saber cuántos hay realmente y la interfaz sería engañosa.
        """
        base = (await client.get(_prints_url(deck, any_card))).json()["facets"]
        filtered = (
            await client.get(_prints_url(deck, any_card, only="full_art"))
        ).json()["facets"]
        assert filtered["total"] == base["total"]
        assert filtered["textless"] == base["textless"]

    async def test_filtering_reduces_total(self, client, deck, any_card):
        base = (await client.get(_prints_url(deck, any_card))).json()
        filtered = (
            await client.get(_prints_url(deck, any_card, only="full_art"))
        ).json()
        assert filtered["total"] <= base["total"]

    async def test_text_query_narrows_results(self, client, deck, any_card):
        base = (await client.get(_prints_url(deck, any_card))).json()
        narrowed = (
            await client.get(_prints_url(deck, any_card, q="zzzznoexiste"))
        ).json()
        assert narrowed["total"] == 0
        assert narrowed["total"] <= base["total"]


class TestThumbnailService:
    def test_thumb_path_mirrors_art_structure(self):
        source = PATHS.art_dir / "ab" / "cd1234.png"
        target = thumbnails.thumb_path_for(source)
        assert target.suffix == ".webp"
        assert target.is_relative_to(PATHS.thumbs_dir)
        # Se conserva el subdirectorio: meter decenas de miles de ficheros en
        # una sola carpeta degrada mucho algunos sistemas de ficheros.
        assert "ab" in target.parts

    def test_external_art_gets_bucketed(self):
        target = thumbnails.thumb_path_for(Path("/tmp/whatever/Sol Ring.png"))
        assert target.is_relative_to(PATHS.thumbs_dir)
        assert "_external" in target.parts

    def test_url_normalizes_windows_separators(self):
        url = thumbnails.url_for_relative("ab\\cd1234.png")
        assert url == "/api/thumb/ab/cd1234.png"
        assert "\\" not in url

    def test_url_does_not_double_slash(self):
        assert thumbnails.url_for_relative("/ab/cd.png") == "/api/thumb/ab/cd.png"

    async def test_missing_source_returns_none(self):
        assert await thumbnails.ensure_thumb(PATHS.art_dir / "nope.png") is None

    async def test_unsupported_extension_returns_none(self, tmp_path):
        weird = tmp_path / "art.tiff.xyz"
        weird.write_bytes(b"whatever")
        assert await thumbnails.ensure_thumb(weird) is None

    @pytest.mark.skipif(
        not thumbnails.pillow_available(), reason="Pillow no está instalado"
    )
    async def test_generates_a_smaller_webp(self):
        from PIL import Image

        source = PATHS.art_dir / "thumbtest.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        # Una imagen del tamaño real de una carta de Scryfall.
        Image.new("RGB", (745, 1040), (30, 60, 120)).save(source)

        thumb = await thumbnails.ensure_thumb(source)
        assert thumb is not None and thumb.exists()
        assert thumb.suffix == ".webp"

        with Image.open(thumb) as im:
            assert im.width <= thumbnails.THUMB_WIDTH
            assert im.height <= thumbnails.THUMB_HEIGHT
        assert thumb.stat().st_size < source.stat().st_size, (
            "La miniatura debe pesar menos que el original — es su razón de ser"
        )

    @pytest.mark.skipif(
        not thumbnails.pillow_available(), reason="Pillow no está instalado"
    )
    async def test_second_call_reuses_the_file(self):
        from PIL import Image

        source = PATHS.art_dir / "reuse.png"
        Image.new("RGB", (400, 560), (10, 10, 10)).save(source)

        first = await thumbnails.ensure_thumb(source)
        stamp = first.stat().st_mtime_ns
        second = await thumbnails.ensure_thumb(source)
        assert second == first
        assert second.stat().st_mtime_ns == stamp, "No debe regenerarse sin motivo"

    @pytest.mark.skipif(
        not thumbnails.pillow_available(), reason="Pillow no está instalado"
    )
    async def test_corrupt_image_degrades_instead_of_raising(self):
        """Una imagen rota no puede tumbar la carga de la rejilla entera."""
        bad = PATHS.art_dir / "corrupt.png"
        bad.write_bytes(b"esto no es un PNG")
        assert await thumbnails.ensure_thumb(bad) is None


class TestThumbnailEndpoint:
    async def test_unknown_art_is_404(self, client):
        r = await client.get("/api/thumb/no/existe.png")
        assert r.status_code == 404

    @pytest.mark.parametrize("attack", [
        "../../../etc/passwd",
        "..%2f..%2f..%2fetc%2fpasswd",
        "a/../../../../etc/passwd",
    ])
    async def test_path_traversal_is_blocked(self, client, attack):
        """Sin esta comprobación se podría leer cualquier fichero del disco."""
        r = await client.get(f"/api/thumb/{attack}")
        assert r.status_code in (400, 404), (
            f"Path traversal no bloqueado para {attack!r}: {r.status_code}"
        )
        assert b"root:" not in r.content

    async def test_stats_endpoint_reports_availability(self, client):
        r = await client.get("/api/thumbs/stats")
        assert r.status_code == 200
        body = r.json()
        assert "count" in body and "available" in body

    async def test_clear_endpoint_is_safe_to_call(self, client):
        r = await client.post("/api/thumbs/clear")
        assert r.status_code == 200
        assert "removed" in r.json()

    @pytest.mark.skipif(
        not thumbnails.pillow_available(), reason="Pillow no está instalado"
    )
    async def test_generates_on_demand_and_caches(self, client):
        from PIL import Image

        source = PATHS.art_dir / "ondemand.png"
        Image.new("RGB", (745, 1040), (200, 50, 50)).save(source)

        r = await client.get("/api/thumb/ondemand.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/webp"
        # `immutable` evita incluso la petición condicional al refrescar.
        assert "immutable" in r.headers.get("cache-control", "")
        assert thumbnails.thumb_path_for(source).exists()
