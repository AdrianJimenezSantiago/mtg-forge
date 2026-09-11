"""Tests del modo offline (bulk data) y de la caché de imágenes del PDF.

Cubren los ítems 9 y 11 de la hoja de ruta. Ninguno toca la red: el volcado de
Scryfall se simula con un JSON pequeño de la misma forma que el real.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mpc_forge.services import bulk_data
from mpc_forge.services.pdf_generator import _ImageReaderCache


# --------------------------------------------------------------------------
# Fixtures de datos con la forma exacta que devuelve Scryfall
# --------------------------------------------------------------------------

SIMPLE_CARD = {
    "id": "aaaa-1111",
    "oracle_id": "oracle-sol-ring",
    "name": "Sol Ring",
    "set": "LEA",
    "set_name": "Limited Edition Alpha",
    "collector_number": "270",
    "rarity": "uncommon",
    "lang": "en",
    "layout": "normal",
    "mana_cost": "{1}",
    "cmc": 1.0,
    "type_line": "Artifact",
    "colors": [],
    "color_identity": [],
    "keywords": [],
    "frame": "1993",
    "border_color": "black",
    "full_art": False,
    "textless": False,
    "promo": False,
    "artist": "Mark Tedin",
    "released_at": "1993-08-05",
    "finishes": ["nonfoil"],
    "image_uris": {
        "normal": "https://img/normal.jpg",
        "large": "https://img/large.jpg",
        "png": "https://img/card.png",
    },
}

DFC_CARD = {
    "id": "bbbb-2222",
    "oracle_id": "oracle-delver",
    "name": "Delver of Secrets // Insectile Aberration",
    "set": "ISD",
    "set_name": "Innistrad",
    "collector_number": "51",
    "rarity": "common",
    "lang": "en",
    "layout": "transform",
    "cmc": 1.0,
    "color_identity": ["U"],
    "card_faces": [
        {
            "name": "Delver of Secrets",
            "mana_cost": "{U}",
            "type_line": "Creature — Human Wizard",
            "colors": ["U"],
            "image_uris": {"normal": "https://img/front.jpg"},
        },
        {
            "name": "Insectile Aberration",
            "type_line": "Creature — Human Insect",
            "image_uris": {"normal": "https://img/back.jpg"},
        },
    ],
}

TOKEN_WITH_PARTS = {
    "id": "cccc-3333",
    "oracle_id": "oracle-krenko",
    "name": "Krenko, Mob Boss",
    "set": "M13",
    "set_name": "Magic 2013",
    "collector_number": "138",
    "rarity": "rare",
    "layout": "normal",
    "cmc": 4.0,
    "image_uris": {"normal": "https://img/krenko.jpg"},
    "all_parts": [
        {"id": "tok-1", "name": "Goblin", "component": "token"},
        {"id": "self", "name": "Krenko, Mob Boss", "component": "combo_piece"},
    ],
}

ART_SERIES = {
    "id": "dddd-4444",
    "oracle_id": "oracle-art",
    "name": "Sol Ring // Sol Ring",
    "set": "SLD",
    "layout": "art_series",
    "collector_number": "1",
    "rarity": "common",
}


class TestCardMapping:
    def test_simple_card_maps_every_field(self):
        row = bulk_data.card_to_row(SIMPLE_CARD)
        assert row is not None
        assert row["scryfall_id"] == "aaaa-1111"
        assert row["oracle_id"] == "oracle-sol-ring"
        assert row["name"] == "Sol Ring"
        # El código de set se normaliza a minúsculas: el resto de la app
        # compara siempre en minúscula.
        assert row["set_code"] == "lea"
        assert row["image_png"] == "https://img/card.png"
        assert row["artist"] == "Mark Tedin"

    def test_dfc_takes_front_face_metadata(self):
        row = bulk_data.card_to_row(DFC_CARD)
        assert row["mana_cost"] == "{U}", "El coste debe salir de la cara frontal"
        assert row["type_line"].startswith("Creature")
        assert row["colors"] == "U"

    def test_dfc_captures_back_face(self):
        row = bulk_data.card_to_row(DFC_CARD)
        assert row["back_name"] == "Insectile Aberration"
        assert row["back_image_normal"] == "https://img/back.jpg"

    def test_related_parts_keeps_only_useful_components(self):
        row = bulk_data.card_to_row(TOKEN_WITH_PARTS)
        parts = json.loads(row["related_parts"])
        components = {p["component"] for p in parts}
        assert components == {"token"}, (
            "combo_piece no aporta nada al flujo de impresión y solo ocupa sitio"
        )

    def test_card_without_related_parts_stores_empty_string(self):
        assert bulk_data.card_to_row(SIMPLE_CARD)["related_parts"] == ""

    def test_art_series_is_skipped(self):
        assert bulk_data.card_to_row(ART_SERIES) is None

    def test_card_without_id_is_skipped(self):
        assert bulk_data.card_to_row({"name": "Rota"}) is None

    def test_missing_optional_fields_do_not_raise(self):
        minimal = {"id": "x", "oracle_id": "y", "name": "Z", "layout": "normal"}
        row = bulk_data.card_to_row(minimal)
        assert row is not None
        assert row["cmc"] == 0.0
        assert row["colors"] == ""
        assert row["image_normal"] is None


class TestIncrementalParser:
    """El parser de respaldo para cuando ijson no está instalado."""

    def _parse_all(self, text: str, chunk_size: int) -> list[dict]:
        parser = bulk_data._IncrementalArrayParser()
        out = []
        for i in range(0, len(text), chunk_size):
            out.extend(parser.feed(text[i:i + chunk_size]))
        return out

    def test_parses_a_small_array(self):
        payload = json.dumps([SIMPLE_CARD, TOKEN_WITH_PARTS])
        assert len(self._parse_all(payload, 4096)) == 2

    @pytest.mark.parametrize("chunk_size", [1, 3, 17, 128, 100000])
    def test_result_is_independent_of_chunk_boundaries(self, chunk_size):
        """Los cortes de red caen en sitios arbitrarios, incluso a mitad de
        una cadena o de un carácter de escape."""
        payload = json.dumps([SIMPLE_CARD, DFC_CARD, TOKEN_WITH_PARTS])
        cards = self._parse_all(payload, chunk_size)
        assert len(cards) == 3
        assert [c["id"] for c in cards] == ["aaaa-1111", "bbbb-2222", "cccc-3333"]

    def test_handles_braces_inside_strings(self):
        """Un `{` dentro de una cadena no abre un objeto.

        Los costes de maná de Magic son literalmente `{2}{U}{U}`, así que este
        caso no es hipotético: aparece en casi todas las cartas.
        """
        tricky = {"id": "1", "oracle_id": "o", "name": "Counterspell",
                  "layout": "normal", "mana_cost": "{U}{U}"}
        cards = self._parse_all(json.dumps([tricky]), 5)
        assert len(cards) == 1
        assert cards[0]["mana_cost"] == "{U}{U}"

    def test_handles_escaped_quotes(self):
        tricky = {"id": "1", "oracle_id": "o", "layout": "normal",
                  "name": 'Ach! Hans, Run!', "flavor": 'dijo \\"corre\\"'}
        cards = self._parse_all(json.dumps([tricky]), 7)
        assert len(cards) == 1

    def test_empty_array_yields_nothing(self):
        assert self._parse_all("[]", 1) == []

    def test_buffer_does_not_grow_unbounded(self):
        """El buffer debe vaciarse al emitir cada objeto.

        Sin esto el parser acumularía los 500 MB del volcado en memoria, que es
        justo lo que el streaming pretende evitar.
        """
        parser = bulk_data._IncrementalArrayParser()
        payload = json.dumps([SIMPLE_CARD] * 50)
        consumed = 0
        for i in range(0, len(payload), 512):
            consumed += len(list(parser.feed(payload[i:i + 512])))
        assert consumed == 50
        assert len(parser._buf) < 4096, (
            f"El buffer retiene {len(parser._buf)} caracteres tras procesar "
            f"{len(payload)} — no se está vaciando"
        )


class TestBulkEndpoints:
    async def test_status_reports_local_counts(self, client):
        r = await client.get("/api/bulk/status")
        assert r.status_code == 200
        body = r.json()
        assert "printings" in body
        assert "unique_cards" in body
        assert "progress" in body

    async def test_progress_starts_idle(self, client):
        body = (await client.get("/api/bulk/progress")).json()
        assert body["phase"] == "idle"
        assert body["active"] is False

    async def test_unknown_kind_is_rejected(self, client):
        r = await client.post("/api/bulk/sync?kind=inventado")
        assert r.status_code == 400

    async def test_check_rejects_unknown_kind(self, client):
        r = await client.get("/api/bulk/check?kind=inventado")
        assert r.status_code == 400

    async def test_cancel_with_nothing_running_is_not_an_error(self, client):
        r = await client.post("/api/bulk/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled"] is False


class TestProgressReporting:
    def test_percent_is_zero_without_a_known_total(self):
        p = bulk_data.BulkProgress()
        assert p.to_dict()["percent"] == 0.0

    def test_percent_tracks_bytes(self):
        p = bulk_data.BulkProgress(bytes_total=1000, bytes_downloaded=250)
        assert p.to_dict()["percent"] == 25.0

    @pytest.mark.parametrize("phase,expected", [
        ("idle", False), ("manifest", True), ("downloading", True),
        ("importing", True), ("done", False), ("error", False),
    ])
    def test_active_flag_matches_phase(self, phase, expected):
        assert bulk_data.BulkProgress(phase=phase).to_dict()["active"] is expected


class TestImageReaderCache:
    """Ítem 11: reutilizar el ImageReader para las cartas repetidas."""

    @pytest.fixture
    def sample_image(self, tmp_path) -> Path:
        pytest.importorskip("PIL")
        from PIL import Image
        path = tmp_path / "card.png"
        Image.new("RGB", (100, 140), (20, 40, 80)).save(path)
        return path

    def test_repeated_path_returns_the_same_object(self, sample_image):
        """ReportLab solo deduplica los bytes si recibe el MISMO objeto."""
        cache = _ImageReaderCache()
        first = cache.get(str(sample_image))
        second = cache.get(str(sample_image))
        assert first is second

    def test_counts_hits_and_misses(self, sample_image):
        cache = _ImageReaderCache()
        for _ in range(35):          # 35 tierras básicas del mismo arte
            cache.get(str(sample_image))
        assert cache.misses == 1, "Solo debe decodificarse una vez"
        assert cache.hits == 34

    def test_different_paths_are_separate_entries(self, sample_image, tmp_path):
        from PIL import Image
        other = tmp_path / "other.png"
        Image.new("RGB", (100, 140), (200, 10, 10)).save(other)

        cache = _ImageReaderCache()
        assert cache.get(str(sample_image)) is not cache.get(str(other))
        assert cache.misses == 2

    def test_summary_is_empty_without_images(self):
        assert "sin imágenes" in _ImageReaderCache().summary()

    def test_summary_reports_savings(self, sample_image):
        cache = _ImageReaderCache()
        for _ in range(4):
            cache.get(str(sample_image))
        summary = cache.summary()
        assert "1 imágenes únicas" in summary
        assert "75%" in summary
