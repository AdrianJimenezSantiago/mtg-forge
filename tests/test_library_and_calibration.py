"""Tests de la biblioteca de arte y del asistente de calibración.

La calibración se prueba con especial detalle porque su parte difícil no es el
PDF sino el signo de la corrección: es donde se equivoca todo el mundo, y un
signo invertido produce cartas con el reverso el DOBLE de descentrado en lugar
de arreglarlas.
"""
from __future__ import annotations


import pytest

from mpc_forge.services import art_library, calibration


# ===========================================================================
# Calibración: derivación de offsets
# ===========================================================================

class TestCalibrationSigns:
    def test_no_drift_means_no_correction(self):
        result = calibration.derive_offsets(0, 0)
        assert result.back_offset_x_mm == 0
        assert result.back_offset_y_mm == 0
        assert result.warning == "calibration_already_aligned"

    def test_vertical_correction_is_the_opposite_of_the_drift(self):
        """Si el reverso sube 2 mm, hay que bajarlo 2 mm."""
        result = calibration.derive_offsets(0, 2.0, flip_edge="long")
        assert result.back_offset_y_mm == -2.0

    def test_vertical_correction_works_downwards_too(self):
        result = calibration.derive_offsets(0, -3.5, flip_edge="long")
        assert result.back_offset_y_mm == 3.5

    def test_long_edge_flip_mirrors_the_horizontal_axis(self):
        """En dúplex por borde largo la hoja gira sobre el eje vertical.

        El sistema de coordenadas del reverso queda espejado, así que la
        corrección en X va en el MISMO sentido que la medida, no en el
        contrario. Es el detalle que hace que la gente lo aplique al revés.
        """
        result = calibration.derive_offsets(2.0, 0, flip_edge="long")
        assert result.back_offset_x_mm == 2.0

    def test_short_edge_flip_mirrors_the_vertical_axis_instead(self):
        result = calibration.derive_offsets(2.0, 2.0, flip_edge="short")
        assert result.back_offset_x_mm == -2.0
        assert result.back_offset_y_mm == 2.0

    def test_the_two_flip_modes_differ(self):
        """Si dieran lo mismo, el selector de borde sería decorativo."""
        long_edge = calibration.derive_offsets(2.0, 2.0, flip_edge="long")
        short_edge = calibration.derive_offsets(2.0, 2.0, flip_edge="short")
        assert (long_edge.back_offset_x_mm, long_edge.back_offset_y_mm) != (
            short_edge.back_offset_x_mm, short_edge.back_offset_y_mm
        )

    @pytest.mark.parametrize("flip", ["long", "short"])
    @pytest.mark.parametrize("x,y", [(1.0, 1.0), (-2.5, 3.0), (0.5, -0.5)])
    def test_applying_twice_returns_to_the_start(self, flip, x, y):
        """Medir la deriva de la corrección debe deshacerla.

        Es la propiedad que garantiza que el signo es coherente: si calibras,
        vuelves a imprimir y la nueva deriva es cero, la segunda pasada no debe
        cambiar nada.
        """
        first = calibration.derive_offsets(x, y, flip_edge=flip)
        again = calibration.derive_offsets(
            first.back_offset_x_mm, first.back_offset_y_mm, flip_edge=flip
        )
        assert again.back_offset_x_mm == pytest.approx(x, abs=0.01)
        assert again.back_offset_y_mm == pytest.approx(y, abs=0.01)

    def test_a_huge_drift_is_flagged_as_suspicious(self):
        """Más de un centímetro no es descalibración: es otro problema."""
        result = calibration.derive_offsets(0, 25.0)
        assert result.warning == "calibration_offset_suspicious"

    def test_a_normal_drift_carries_no_warning(self):
        assert calibration.derive_offsets(1.5, -2.0).warning is None

    def test_results_are_rounded_to_two_decimals(self):
        """Ninguna impresora doméstica distingue una centésima de milímetro."""
        result = calibration.derive_offsets(1.23456, -2.98765)
        assert result.back_offset_x_mm == 1.23
        assert result.back_offset_y_mm == 2.99

    def test_the_measurement_is_echoed_back(self):
        """La interfaz muestra qué se midió junto a lo que se aplicará."""
        result = calibration.derive_offsets(1.5, -2.0, flip_edge="short")
        assert result.measured_x_mm == 1.5
        assert result.measured_y_mm == -2.0
        assert result.flip_edge == "short"


class TestCalibrationExplanation:
    def test_explain_flags_when_correction_is_needed(self):
        result = calibration.derive_offsets(2.0, 1.0)
        assert calibration.explain(result)["needs_correction"] is True

    def test_explain_flags_when_already_aligned(self):
        result = calibration.derive_offsets(0, 0)
        assert calibration.explain(result)["needs_correction"] is False

    def test_a_negligible_drift_needs_no_correction(self):
        """0,02 mm está por debajo de lo que cualquier impresora resuelve."""
        result = calibration.derive_offsets(0.02, 0.0)
        assert calibration.explain(result)["needs_correction"] is False

    def test_explanation_key_depends_on_the_flip_edge(self):
        long_edge = calibration.explain(calibration.derive_offsets(1, 1, flip_edge="long"))
        short_edge = calibration.explain(calibration.derive_offsets(1, 1, flip_edge="short"))
        assert long_edge["explanation_key"] != short_edge["explanation_key"]


class TestCalibrationSheet:
    def test_generates_a_two_page_pdf(self, tmp_path):
        path = calibration.build_sheet(tmp_path / "cal.pdf")
        assert path.exists()
        content = path.read_bytes()
        assert content.startswith(b"%PDF")
        assert content.count(b"/Type /Page") >= 2 or content.count(b"/Page") >= 2

    def test_letter_size_is_supported(self, tmp_path):
        path = calibration.build_sheet(tmp_path / "letter.pdf", page_size="letter")
        assert path.exists() and path.stat().st_size > 1000

    def test_an_unknown_size_falls_back_to_a4(self, tmp_path):
        """Un tamaño raro no debe reventar la generación."""
        path = calibration.build_sheet(tmp_path / "weird.pdf", page_size="inventado")
        assert path.exists()

    def test_the_output_directory_is_created(self, tmp_path):
        target = tmp_path / "no" / "existe" / "cal.pdf"
        assert calibration.build_sheet(target).exists()

    def test_the_two_rulers_do_not_collide(self, tmp_path):
        """Las etiquetas de ambos ejes deben poder leerse cerca del origen.

        La primera versión dibujaba las reglas como barras independientes que
        se cruzaban, y en el cuadrante del solape los números quedaban
        ilegibles — justo en el rango de ±5 mm, que es donde caen casi todas
        las derivas reales.

        Se comprueba geométricamente en vez de por inspección visual: se
        reconstruyen las posiciones de las dos etiquetas más próximas entre sí
        y se exige separación suficiente para el cuerpo del texto.
        """
        MM = 1.0
        tick = 3.5 * MM
        # Etiqueta "5" del eje horizontal: colgada bajo la marca.
        horizontal = (5 * MM, -(tick + 5 * MM))
        # Etiqueta "-5" del eje vertical: a la derecha de su marca.
        vertical = (tick + 1.2 * MM, -5 * MM)

        vertical_gap = abs(horizontal[1] - vertical[1])
        assert vertical_gap >= 2.5, (
            f"Las etiquetas '5' y '-5' quedan a {vertical_gap:.1f} mm; el "
            f"texto mide ~2 mm de alto y se solaparían."
        )


# ===========================================================================
# Biblioteca de arte
# ===========================================================================

class TestLibraryFilters:
    def test_no_filters_is_empty(self):
        assert art_library.LibraryFilters().is_empty()

    @pytest.mark.parametrize("field,value", [
        ("query", "sol ring"),
        ("expansion_code", "dmu"),
        ("card_type", "TOKEN"),
    ])
    def test_any_filter_makes_it_non_empty(self, field, value):
        assert not art_library.LibraryFilters(**{field: value}).is_empty()

    def test_list_filters_count_too(self):
        assert not art_library.LibraryFilters(source_ids=[1]).is_empty()
        assert not art_library.LibraryFilters(variants=["full_art"]).is_empty()


class TestSortOptions:
    def test_the_default_sort_exists(self):
        assert art_library.DEFAULT_SORT in art_library.SORT_OPTIONS

    @pytest.mark.parametrize("name", list(art_library.SORT_OPTIONS))
    def test_every_sort_has_a_total_order(self, name):
        """Sin desempate por id, la paginación duplica o se salta elementos.

        Dos filas con el mismo nombre pueden salir en distinto orden entre dos
        consultas, y entonces la página 2 solapa con la 1.
        """
        clauses = art_library.SORT_OPTIONS[name]
        last = clauses[-1]
        assert "id" in str(last), (
            f"El orden '{name}' no desempata por id y no es estable al paginar"
        )


class TestVariantFlags:
    def test_the_expected_variants_are_present(self):
        expected = {
            "full_art", "borderless", "extended", "showcase",
            "retro", "textless", "promo", "alt_art",
        }
        assert expected == set(art_library.VARIANT_FLAGS)

    def test_page_size_has_a_ceiling(self):
        """Sin tope, `limit=999999` traería el índice entero de golpe."""
        assert art_library.MAX_PAGE_SIZE <= 500
        assert art_library.DEFAULT_PAGE_SIZE <= art_library.MAX_PAGE_SIZE


# ===========================================================================
# Endpoints
# ===========================================================================

class TestSerializedUrls:
    """Las miniaturas de la rejilla tienen que apuntar a algún sitio.

    La columna `thumb_url` de `IndexedArt` está vacía en la mayoría de las
    filas: solo se rellena cuando el indexador la recibe del API de Drive, y
    por el camino de scraping no llega. Leerla directamente dejaba `src=""` y
    la biblioteca aparecía entera sin imágenes.
    """

    class _Row:
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
        thumb_url = None          # ← el caso mayoritario
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

    def _serialized(self, **overrides):
        row = self._Row()
        for key, value in overrides.items():
            setattr(row, key, value)
        return art_library._serialize(row, {1: "Mi Drive"})

    def test_thumb_url_is_derived_when_the_column_is_empty(self):
        data = self._serialized()
        assert data["thumb_url"], "La miniatura no puede quedar vacía"
        assert "FILE123" in data["thumb_url"]

    def test_download_url_is_derived_when_the_column_is_empty(self):
        data = self._serialized()
        assert data["download_url"]
        assert "FILE123" in data["download_url"]

    def test_a_stored_url_takes_precedence(self):
        """Si el indexador sí la trajo, se respeta."""
        data = self._serialized(thumb_url="https://ejemplo/miniatura.png")
        assert data["thumb_url"] == "https://ejemplo/miniatura.png"

    def test_variants_are_listed_from_the_boolean_columns(self):
        assert set(self._serialized()["variants"]) == {"full_art", "retro"}

    def test_tags_are_split_and_empties_dropped(self):
        assert self._serialized(tags="")["tags"] == []
        assert self._serialized(tags="a,,b")["tags"] == ["a", "b"]

    def test_the_source_name_is_resolved(self):
        assert self._serialized()["source_name"] == "Mi Drive"

    def test_an_unknown_source_does_not_raise(self):
        """Un drive borrado deja filas huérfanas hasta que se reindexa."""
        assert self._serialized(source_id=999)["source_name"] == ""


class TestLibraryEndpoints:
    async def test_overview_on_an_empty_index(self, client):
        r = await client.get("/api/library/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["total_arts"] == 0
        assert body["is_empty"] is True

    async def test_every_item_carries_a_usable_thumbnail(self, client):
        """Ninguna fila devuelta puede llegar sin miniatura."""
        body = (await client.get("/api/library/browse")).json()
        for item in body["items"]:
            assert item["thumb_url"], (
                f"{item['filename']} llega sin miniatura: la rejilla mostraría "
                f"un hueco"
            )

    async def test_browse_returns_a_paginated_envelope(self, client):
        r = await client.get("/api/library/browse")
        assert r.status_code == 200
        body = r.json()
        for key in ("items", "total", "offset", "limit", "has_more", "sort"):
            assert key in body

    async def test_browse_rejects_an_unknown_sort(self, client):
        r = await client.get("/api/library/browse?sort=drop_table")
        assert r.status_code == 400

    async def test_browse_rejects_unknown_variants(self, client):
        r = await client.get("/api/library/browse?variants=inventada")
        assert r.status_code == 400

    async def test_browse_accepts_every_real_variant(self, client):
        joined = ",".join(art_library.VARIANT_FLAGS)
        r = await client.get(f"/api/library/browse?variants={joined}")
        assert r.status_code == 200

    async def test_browse_caps_the_page_size(self, client):
        assert (await client.get(
            "/api/library/browse?limit=99999"
        )).status_code == 422

    async def test_a_non_numeric_source_id_is_ignored_not_fatal(self, client):
        """El filtro viaja en la URL, que el usuario puede editar o compartir."""
        r = await client.get("/api/library/browse?sources=abc,1")
        assert r.status_code == 200

    async def test_facets_expose_every_group(self, client):
        r = await client.get("/api/library/facets")
        assert r.status_code == 200
        body = r.json()
        for key in ("by_source", "by_variant", "by_expansion", "by_card_type"):
            assert key in body

    async def test_facets_include_all_variants_even_at_zero(self, client):
        body = (await client.get("/api/library/facets")).json()
        assert set(body["by_variant"]) == set(art_library.VARIANT_FLAGS)

    async def test_cards_view_groups_by_name(self, client):
        r = await client.get("/api/library/cards")
        assert r.status_code == 200
        assert "cards" in r.json()

    async def test_variants_endpoint_avoids_duplicating_the_list(self, client):
        body = (await client.get("/api/library/variants")).json()
        assert set(body["variants"]) == set(art_library.VARIANT_FLAGS)
        assert set(body["sorts"]) == set(art_library.SORT_OPTIONS)


class TestCalibrationEndpoints:
    async def test_derive_returns_offsets(self, client):
        r = await client.post("/api/calibration/derive", json={
            "measured_x_mm": 2.0, "measured_y_mm": -1.5, "flip_edge": "long",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["back_offset_x_mm"] == 2.0
        assert body["back_offset_y_mm"] == 1.5
        assert body["needs_correction"] is True

    async def test_derive_rejects_absurd_measurements(self, client):
        r = await client.post("/api/calibration/derive", json={
            "measured_x_mm": 500, "measured_y_mm": 0,
        })
        assert r.status_code == 422

    async def test_derive_rejects_an_unknown_flip_edge(self, client):
        r = await client.post("/api/calibration/derive", json={
            "measured_x_mm": 1, "measured_y_mm": 1, "flip_edge": "diagonal",
        })
        assert r.status_code == 422

    async def test_sheet_returns_a_pdf(self, client):
        r = await client.get("/api/calibration/sheet")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")

    async def test_sheet_is_not_cached(self, client):
        """Se regenera según el tamaño y el borde elegidos: servir una versión
        cacheada con la configuración anterior arruinaría la calibración."""
        r = await client.get("/api/calibration/sheet")
        assert "no-store" in r.headers.get("cache-control", "")

    async def test_sheet_rejects_an_unsupported_page_size(self, client):
        r = await client.get("/api/calibration/sheet?page_size=a0")
        assert r.status_code == 400
