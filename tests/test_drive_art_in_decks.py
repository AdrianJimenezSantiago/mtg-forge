"""Tests diagnósticos: arte alternativo de drives en la vista de mazo.

Reproduce el flujo completo que sigue la UI cuando el usuario abre el art
picker para una carta como Sol Ring:

  1. Drives se indexan → IndexedArt se puebla.
  2. FTS5 se sincroniza via triggers.
  3. `/api/drives/stats` devuelve total_files > 0 (guard del frontend).
  4. `/api/drives/search?q=Sol Ring` devuelve los hits indexados.
  5. El art picker del mazo muestra esos hits como opciones `kind: 'drive'`.

El bug reportado: Sol Ring no muestra NINGÚN arte de drives, a pesar de
que hay docenas de archivos indexados con ese nombre.

Causa raíz encontrada: gdrive_search.py SIEMPRE generaba URLs de Google
Drive para thumb_url/download_url, ignorando las columnas almacenadas en
IndexedArt. Para sources no-gdrive (local-folder, http-listing, s3), esto
producía URLs rotas → thumbnails no cargan → parece que no hay arte.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_png(name: str = "x") -> bytes:
    """Mínimo header PNG válido para que el MIME check lo acepte."""
    return b"\x89PNG\r\n\x1a\n" + name.encode()


def _fake_jpg() -> bytes:
    return b"\xff\xd8\xff\xe0"


# ---------------------------------------------------------------------------
# Fixture: source local indexado con arte variado de Sol Ring
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def indexed_local_source(client, tmp_path):
    """Crea un local-folder con muchos artes de Sol Ring y lo indexa.

    Simula lo que el usuario tendría tras indexar un drive de MPCFill con
    docenas de variantes de Sol Ring: full art, borderless, retro, alt,
    nombres con paréntesis, brackets, artistas, etc.
    """
    files = [
        # Nombres típicos en drives de MPCFill
        "Sol Ring.png",
        "Sol Ring (Full Art).png",
        "Sol Ring (Borderless).png",
        "Sol Ring (Retro).png",
        "Sol Ring (Alt Art).png",
        "Sol Ring (Anime).png",
        "Sol Ring (Textless).png",
        "Sol Ring - by Daubrez.png",
        "Sol Ring [C21 263].png",
        "Sol Ring (Extended).png",
        "Sol Ring (Showcase).png",
        # OJO: aquí NO se puede usar "|" como separador aunque sea un patrón
        # real en Drive. Windows lo prohíbe en nombres de fichero (junto con
        # < > : " / \\ ? *) y `write_bytes` falla con EINVAL. La normalización
        # de "|" se sigue cubriendo en el test de normalize_filename, que
        # trabaja con cadenas y no toca el sistema de ficheros.
        "Sol Ring ~ Fan Art.jpg",
        # Subcarpeta
        "Sol Ring (Promo).png",
        # Otras cartas (no deben matchear Sol Ring)
        "Forest.png",
        "Forest (Full Art).png",
        "Command Tower.png",
        "Cursed Sol Ring.png",   # nombre distinto, no debería ser match 100
    ]
    # Caracteres que Windows prohíbe en nombres de fichero. Comprobarlo aquí
    # da un error legible en lugar de un EINVAL a veinte líneas de
    # profundidad dentro de pathlib. Los tests corren en windows-latest.
    illegal = set('<>:"|?*')
    for f in files:
        assert not (illegal & set(f)), (
            f"El fixture usa {f!r}, con un carácter ilegal en Windows."
        )

    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_fake_png(f) if f.endswith(".png") else _fake_jpg())

    # Subcarpeta con un arte más
    sub = tmp_path / "Retro Frame"
    sub.mkdir(exist_ok=True)
    (sub / "Sol Ring (Old Border).png").write_bytes(_fake_png("old"))

    # Añadir como local-folder source
    r = await client.post("/api/art-sources/", json={
        "name": "Test Drive Sol Ring",
        "url": str(tmp_path),
    })
    assert r.status_code in (200, 201), r.text
    src_id = r.json()["id"]
    assert r.json()["source_type"] == "local-folder"

    # Indexar (síncrono con ?wait=true)
    r = await client.post(f"/api/art-sources/{src_id}/index?wait=true")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["error"] is None, f"Error de indexación: {result['error']}"
    # Debe haber indexado las imágenes (al menos las Sol Ring)
    assert result["files_added"] > 0, "No se indexó ningún archivo"

    return {"source_id": src_id, "tmp_path": tmp_path, "result": result}


# ===========================================================================
# TEST 1: Stats endpoint — el guard del frontend
# ===========================================================================

class TestDriveStatsGuard:
    """El frontend solo busca en drives si `driveIndexStats.total_files > 0`.

    Si este endpoint devuelve 0, el art picker NUNCA hace la búsqueda y el
    usuario ve 0 artes de drives — exactamente el síntoma reportado.
    """

    async def test_stats_zero_before_indexing(self, client):
        """Sin indexar nada, stats debe devolver 0 (guard bloqueará búsqueda)."""
        r = await client.get("/api/drives/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_files"] == 0
        assert data["sources_indexed"] == 0

    async def test_stats_positive_after_indexing(self, client, indexed_local_source):
        """Después de indexar, total_files debe ser > 0."""
        r = await client.get("/api/drives/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_files"] > 0, (
            "BUG: total_files = 0 después de indexar. "
            "El frontend nunca buscará en drives."
        )
        assert data["sources_indexed"] >= 1


# ===========================================================================
# TEST 2: Búsqueda directa — el endpoint que usa el art picker
# ===========================================================================

class TestDriveSearchForSolRing:
    """Reproduce exactamente la llamada que hace `_loadDrivesForPicker('Sol Ring')`."""

    async def test_search_returns_results(self, client, indexed_local_source):
        """La búsqueda de 'Sol Ring' debe devolver múltiples hits."""
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) > 0, (
            "BUG CONFIRMADO: /api/drives/search?q=Sol+Ring devuelve 0 resultados "
            "a pesar de que hay archivos indexados con ese nombre."
        )

    async def test_search_returns_correct_count(self, client, indexed_local_source):
        """Debe encontrar TODOS los artes de Sol Ring, no solo uno o dos."""
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        hits = r.json()
        # Tenemos ~14 archivos Sol Ring (excluyendo "Cursed Sol Ring")
        sol_ring_hits = [h for h in hits if h["score"] == 100]
        assert len(sol_ring_hits) >= 10, (
            f"BUG: Solo {len(sol_ring_hits)} exact matches para Sol Ring, "
            f"esperábamos >=10. Hits totales: {len(hits)}. "
            f"Scores: {[(h['filename'], h['score']) for h in hits]}"
        )

    async def test_search_excludes_false_positives(self, client, indexed_local_source):
        """'Cursed Sol Ring' no debe ser un match exacto (score 100) para 'Sol Ring'."""
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        hits = r.json()
        cursed = [h for h in hits if "Cursed" in h["filename"]]
        for h in cursed:
            assert h["score"] < 100, (
                f"False positive: '{h['filename']}' scored {h['score']} "
                "para query 'Sol Ring'"
            )

    async def test_search_has_thumbs_and_download_urls(self, client, indexed_local_source):
        """Cada hit debe tener thumb_url y download_url (el frontend los necesita)."""
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        hits = r.json()
        assert len(hits) > 0
        for h in hits:
            assert h.get("thumb_url") or h.get("download_url"), (
                f"Hit sin URLs: {h['filename']}"
            )

    async def test_search_includes_tags(self, client, indexed_local_source):
        """Los tags extraídos deben estar presentes (para badges en la UI)."""
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        hits = r.json()
        # El archivo "Sol Ring (Full Art).png" debe tener is_full_art=True
        full_art = [h for h in hits if h.get("is_full_art")]
        assert len(full_art) >= 1, (
            "BUG: Ningún hit con is_full_art=True. Tags no se están extrayendo "
            f"o no se pasan al search result. First hit: {hits[0] if hits else 'N/A'}"
        )

    async def test_search_with_tag_filter(self, client, indexed_local_source):
        """El filtro tags_include debe funcionar (el frontend lo usa)."""
        r = await client.get(
            "/api/drives/search?q=Sol+Ring&limit=100&tags_include=full_art"
        )
        hits = r.json()
        assert len(hits) >= 1, "Filtro full_art no devuelve resultados"
        assert all(h["is_full_art"] for h in hits), "Filtro full_art no funciona"


# ===========================================================================
# TEST 3: Normalización — si el normalizador falla, nada matchea
# ===========================================================================

class TestNormalizationForSearch:
    """Verifica que la normalización de filenames indexados coincide con la
    normalización del query del usuario.

    Si hay un drift (el query se normaliza distinto al filename), la búsqueda
    exacta devuelve 0 hits.
    """

    def test_filename_normalization_consistency(self):
        """normalize_filename aplicado a filenames y al card name debe dar lo mismo."""
        from mpc_forge.services.gdrive_indexer import normalize_filename

        # El query del usuario
        card_name = "Sol Ring"
        q_norm = normalize_filename(card_name)
        assert q_norm == "sol ring", f"Query normalizado: {q_norm!r}"

        # Los filenames del drive
        filenames = [
            "Sol Ring.png",
            "Sol Ring (Full Art).png",
            "Sol Ring (Borderless).png",
            "Sol Ring - by Daubrez.png",
            "Sol Ring [C21 263].png",
            "Sol Ring | Fan Art.jpg",
            "Sol Ring (Daubrez Borderless).png",
        ]
        for fn in filenames:
            fn_norm = normalize_filename(fn)
            assert fn_norm == "sol ring", (
                f"BUG: normalize_filename({fn!r}) = {fn_norm!r}, "
                f"debería ser 'sol ring'. No hará match con el query."
            )

    def test_diacritics_normalization(self):
        """Cartas con diacríticos deben matchear sin acentos y viceversa."""
        from mpc_forge.services.gdrive_indexer import normalize_filename

        assert normalize_filename("Jayā Ballard.png") == "jaya ballard"
        assert normalize_filename("Jaya Ballard") == "jaya ballard"
        assert normalize_filename("Æther Vial.png") == "aether vial"
        assert normalize_filename("Aether Vial") == "aether vial"


# ===========================================================================
# TEST 4: FTS5 search path — el path rápido que usan la mayoría de usuarios
# ===========================================================================

class TestFTS5SearchPath:
    """FTS5 es el path preferido. Si está mal configurado o desincronizado,
    devuelve 0 resultados y el fallback LIKE podría no activarse.
    """

    async def test_fts5_available_after_init(self, client):
        """FTS5 debe estar disponible (SQLite moderno)."""
        r = await client.get("/api/drives/stats")
        data = r.json()
        # En la mayoría de entornos modernos, FTS5 está disponible
        # Si no lo está, la búsqueda cae a LIKE — sigue funcionando pero más lento
        if not data.get("fts5_available", False):
            pytest.skip("FTS5 no disponible en esta build de SQLite")

    async def test_fts5_synced_after_indexing(self, client, indexed_local_source):
        """Después de indexar, FTS5 debe tener las mismas filas que indexed_art."""
        from sqlalchemy import text

        from mpc_forge.db import session_scope

        async with session_scope() as db:
            real_count = (await db.execute(text(
                "SELECT COUNT(*) FROM indexed_art"
            ))).scalar()
            try:
                fts_count = (await db.execute(text(
                    "SELECT COUNT(*) FROM indexed_art_fts"
                ))).scalar()
            except Exception:
                pytest.skip("FTS5 no disponible")

        assert fts_count == real_count, (
            f"BUG: FTS5 desincronizado. indexed_art tiene {real_count} filas, "
            f"pero indexed_art_fts tiene {fts_count}. "
            "Los triggers no están funcionando correctamente."
        )

    async def test_fts5_match_sol_ring(self, client, indexed_local_source):
        """Consulta FTS5 directa para 'Sol Ring'."""
        from sqlalchemy import text

        from mpc_forge.db import session_scope

        async with session_scope() as db:
            try:
                rows = (await db.execute(text(
                    "SELECT COUNT(*) FROM indexed_art_fts "
                    "WHERE indexed_art_fts MATCH '\"sol\" \"ring\"*'"
                ))).scalar()
            except Exception as e:
                pytest.skip(f"FTS5 no disponible: {e}")

        assert rows > 0, (
            "BUG: FTS5 MATCH '\"sol\" \"ring\"*' devuelve 0 filas. "
            "Los datos no se están indexando en la tabla virtual FTS5. "
            "Posible causa: triggers no creados o content= desincronizado."
        )

    async def test_fts5_join_returns_full_data(self, client, indexed_local_source):
        """El JOIN entre FTS5 y indexed_art debe devolver datos completos.

        FTS5 prefix match devuelve también 'cursed sol ring' (contiene ambos
        tokens), así que solo verificamos que HAY resultados con filename y
        source_name. El scoring posterior filtra los falsos positivos.
        """
        from sqlalchemy import text

        from mpc_forge.db import session_scope

        async with session_scope() as db:
            try:
                rows = (await db.execute(text(
                    "SELECT ia.filename, ia.name_normalized, ia.file_id, "
                    "       s.name AS source_name "
                    "FROM indexed_art_fts "
                    "JOIN indexed_art AS ia ON ia.id = indexed_art_fts.rowid "
                    "JOIN art_sources AS s ON s.id = ia.source_id "
                    "WHERE indexed_art_fts MATCH '\"sol\" \"ring\"*' "
                    "LIMIT 20"
                ))).fetchall()
            except Exception as e:
                pytest.skip(f"FTS5 no disponible: {e}")

        assert len(rows) > 0, (
            "BUG: FTS5 JOIN devuelve 0 resultados a pesar de tener datos."
        )
        # Verificar que los campos no son NULL
        for row in rows:
            assert row[0], f"filename vacío: {row}"
            assert row[1], f"name_normalized vacío: {row}"
            assert row[3], f"source_name vacío: {row}"
        # Al menos algunos deben ser exact match "sol ring"
        exact = [r for r in rows if r[1] == "sol ring"]
        assert len(exact) >= 5, (
            f"Solo {len(exact)} exact matches 'sol ring' en FTS5 JOIN. "
            f"Resultados: {[(r[0], r[1]) for r in rows]}"
        )


# ===========================================================================
# TEST 5: LIKE fallback path — para builds sin FTS5
# ===========================================================================

class TestLIKEFallbackPath:
    """Si FTS5 no está disponible, la búsqueda cae al path LIKE tri-fase."""

    async def test_like_exact_match(self, client, indexed_local_source):
        """Forzar búsqueda sin FTS5 directamente en el search module."""
        from mpc_forge.db import session_scope
        from mpc_forge.services import gdrive_search

        # Temporalmente deshabilitar FTS5 para probar el fallback
        original_cache = gdrive_search._fts5_available_cache
        gdrive_search._fts5_available_cache = False
        try:
            async with session_scope() as db:
                results = await gdrive_search.search(db, "Sol Ring", limit=100)
        finally:
            gdrive_search._fts5_available_cache = original_cache

        assert len(results) > 0, (
            "BUG: El fallback LIKE también devuelve 0 resultados para 'Sol Ring'."
        )
        exact = [r for r in results if r.score == 100]
        assert len(exact) >= 5, (
            f"Solo {len(exact)} exact matches en modo LIKE. "
            f"Scores: {[(r.filename, r.score) for r in results[:10]]}"
        )


# ===========================================================================
# TEST 6: Scoring — _score_match
# ===========================================================================

class TestScoring:
    """Verifica que el scoring no descarte Sol Ring matches legítimos."""

    def test_exact_match(self):
        from mpc_forge.services.gdrive_search import _score_match
        assert _score_match("sol ring", "sol ring") == 100

    def test_false_positive_rejection(self):
        from mpc_forge.services.gdrive_search import _score_match
        # "Cursed Sol Ring" → noise ratio 1/2 = 0.5 → score ~60
        s = _score_match("sol ring", "cursed sol ring")
        assert s < 100, "Cursed Sol Ring no debería ser score 100"

    def test_superset_name_low_score(self):
        from mpc_forge.services.gdrive_search import _score_match
        # "sol" vs "sol ring" → noise 1/1 = 1.0 → score ~40 (debajo de MIN_SCORE 55)
        s = _score_match("sol", "sol ring")
        assert s <= 55, f"'sol' vs 'sol ring' scored {s}, debería ser bajo"

    def test_longer_query_with_extra_token(self):
        from mpc_forge.services.gdrive_search import _score_match
        # "bruna the fading light" vs "bruna the fading light retro" → 1/4 = 0.25 → ~90
        s = _score_match("bruna the fading light", "bruna the fading light retro")
        assert s >= 85


# ===========================================================================
# TEST 7: FTS5 query escaping — _fts_escape
# ===========================================================================

class TestFTSEscaping:
    """El query FTS5 mal escapado puede causar errores SQL o 0 resultados."""

    def test_basic_escape(self):
        from mpc_forge.services.gdrive_search import _fts_escape
        assert _fts_escape("sol ring") == '"sol" "ring"*'

    def test_single_word(self):
        from mpc_forge.services.gdrive_search import _fts_escape
        assert _fts_escape("forest") == '"forest"*'

    def test_special_chars_in_name(self):
        from mpc_forge.services.gdrive_search import _fts_escape
        # Nombres con comillas no deben romper el query
        result = _fts_escape('sol "ring"')
        # Debe seguir siendo valid FTS5 syntax
        assert '"' in result

    def test_empty_query(self):
        from mpc_forge.services.gdrive_search import _fts_escape
        assert _fts_escape("") == ""
        assert _fts_escape("   ") == ""


# ===========================================================================
# TEST 8: Integración completa deck → art picker → drives
# ===========================================================================

class TestDeckArtPickerDriveIntegration:
    """Simula el flujo completo: crear mazo con Sol Ring, abrir el art picker,
    y verificar que aparecen artes de drives.
    """

    async def test_full_flow_sol_ring(self, client, deck, indexed_local_source):
        """Flujo completo: el art picker de Sol Ring muestra artes de drives."""
        # 1. El deck fixture ya tiene Sol Ring. Encontremos su card_id.
        r = await client.get(f"/api/decks/{deck['id']}")
        assert r.status_code == 200
        cards = r.json()["cards"]
        sol_ring = next((c for c in cards if c["name"] == "Sol Ring"), None)
        assert sol_ring is not None, "Sol Ring no encontrado en el mazo"

        # 2. El frontend primero checkea stats (guard)
        r = await client.get("/api/drives/stats")
        stats = r.json()
        assert stats["total_files"] > 0, (
            "Guard falla: total_files = 0. El frontend nunca buscará."
        )

        # 3. El frontend llama a /prints para las opciones oficiales + custom
        r = await client.get(
            f"/api/decks/{deck['id']}/cards/{sol_ring['id']}/prints"
        )
        assert r.status_code == 200
        page = r.json()
        # /prints devuelve un sobre paginado: {items, custom, total, facets…}.
        # Los artes de drives NO entran aquí — tienen su propia búsqueda.
        assert "items" in page and "total" in page, (
            "El endpoint de prints debe devolver un sobre paginado"
        )
        all_options = page["items"] + page["custom"]
        drive_in_prints = [p for p in all_options if p.get("kind") == "drive"]
        assert len(drive_in_prints) == 0, (
            "Los drives NO deben aparecer en /prints — tienen su propia búsqueda"
        )

        # 4. El frontend llama /drives/search con el nombre de la carta
        # Esto es EXACTAMENTE lo que hace _loadDrivesForPicker(card.name)
        card_name = sol_ring["name"]
        r = await client.get(
            f"/api/drives/search?q={card_name}&limit=100"
        )
        assert r.status_code == 200
        hits = r.json()

        # === ESTE ES EL ASSERTION CLAVE ===
        assert len(hits) > 0, (
            f"BUG CONFIRMADO: /api/drives/search?q={card_name} devuelve 0 "
            f"resultados. El art picker mostrará 0 artes de drives. "
            f"Stats dice {stats['total_files']} archivos indexados. "
            f"Algo está roto entre indexación y búsqueda."
        )

        # 5. Verificar que los hits son Sol Ring (no Forest, etc.)
        for h in hits:
            assert "sol ring" in h["filename"].lower() or h["score"] < 100, (
                f"False positive: {h['filename']} matched con score {h['score']}"
            )

    async def test_art_picker_custom_arts_available_count(self, client, deck, indexed_local_source):
        """El campo custom_arts_available en deck detail NO incluye drives.

        Este campo es solo para local custom arts. El frontend usa un endpoint
        separado para drives. Verificamos que NO se confunden.
        """
        r = await client.get(f"/api/decks/{deck['id']}")
        cards = r.json()["cards"]
        sol_ring = next(c for c in cards if c["name"] == "Sol Ring")
        # custom_arts_available cuenta solo CustomArt (local), no IndexedArt (drives)
        # Así que debería ser 0 (no hemos añadido custom arts locales)
        assert sol_ring["custom_arts_available"] == 0, (
            "custom_arts_available debería ser 0 (solo custom local, no drives)"
        )


# ===========================================================================
# TEST 9: Múltiples sources — verificar que todos se buscan
# ===========================================================================

class TestMultipleSourceSearch:
    """Si el usuario tiene varios drives indexados, la búsqueda debe buscar
    en TODOS (a menos que se filtre por source_id).
    """

    async def test_search_across_multiple_sources(self, client, tmp_path):
        """Crear 2 sources y verificar que la búsqueda los cruza."""
        # Source 1
        d1 = tmp_path / "drive1"
        d1.mkdir()
        (d1 / "Sol Ring.png").write_bytes(_fake_png("d1"))
        (d1 / "Sol Ring (Full Art).png").write_bytes(_fake_png("d1fa"))

        r = await client.post("/api/art-sources/", json={
            "name": "Drive 1", "url": str(d1),
        })
        s1_id = r.json()["id"]

        # Source 2
        d2 = tmp_path / "drive2"
        d2.mkdir()
        (d2 / "Sol Ring (Retro).png").write_bytes(_fake_png("d2"))
        (d2 / "Sol Ring (Borderless).png").write_bytes(_fake_png("d2b"))

        r = await client.post("/api/art-sources/", json={
            "name": "Drive 2", "url": str(d2),
        })
        s2_id = r.json()["id"]

        # Indexar ambos
        for sid in (s1_id, s2_id):
            r = await client.post(f"/api/art-sources/{sid}/index?wait=true")
            assert r.status_code == 200, r.text

        # Buscar sin filtro de source
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
        hits = r.json()
        source_ids = {h["source_id"] for h in hits}
        assert len(source_ids) >= 2, (
            f"BUG: Solo se busca en {len(source_ids)} source(s). "
            f"Source IDs encontrados: {source_ids}. "
            "La búsqueda debe cruzar TODOS los drives indexados."
        )

        # Buscar con filtro de source
        r = await client.get(f"/api/drives/search?q=Sol+Ring&source_id={s1_id}")
        hits_filtered = r.json()
        assert all(h["source_id"] == s1_id for h in hits_filtered), (
            "El filtro source_id no funciona correctamente"
        )


# ===========================================================================
# TEST 10: URLs correctas para sources no-gdrive (BUG FIX verificación)
# ===========================================================================

class TestSearchURLsForNonGdriveSources:
    """Verifica el fix del bug: gdrive_search.py debe usar las columnas
    thumb_url/download_url de IndexedArt cuando existen (sources no-gdrive),
    y caer al formato Google Drive solo cuando son NULL (sources gdrive).

    Antes del fix, SIEMPRE generaba URLs de Google Drive, produciendo URLs
    rotas para local-folder/http-listing/s3 → thumbnails no cargan.
    """

    async def test_local_folder_urls_not_gdrive_format(self, client, indexed_local_source):
        """Los hits de local-folder deben tener URLs que apunten al server local,
        NO a drive.google.com.
        """
        r = await client.get("/api/drives/search?q=Sol+Ring&limit=10")
        hits = r.json()
        assert len(hits) > 0, "No hay hits para verificar"

        for h in hits:
            # Un source local-folder genera URLs como /local-source/{id}/{file_id}
            # NO debe ser https://drive.google.com/thumbnail?id=...
            # El file_id de local-folder es un base64 de la ruta relativa,
            # que no es un ID de Google Drive válido.
            thumb = h.get("thumb_url", "")
            download = h.get("download_url", "")
            # Las URLs correctas para local-folder empiezan con /local-source/
            # o son rutas generadas por el source_type, NO google drive URLs.
            assert "drive.google.com" not in thumb, (
                f"BUG (pre-fix): thumb_url '{thumb}' para source local-folder "
                f"usa formato Google Drive. Archivo: {h['filename']}. "
                "Debe usar la URL almacenada en IndexedArt.thumb_url."
            )

    async def test_search_uses_stored_urls_when_available(self, client, indexed_local_source):
        """Verificación directa: las URLs del search result deben coincidir
        con las almacenadas en IndexedArt, no con las generadas por _thumb_url().
        """
        from sqlalchemy import select

        from mpc_forge.db import session_scope
        from mpc_forge.models import IndexedArt

        # Leer las URLs almacenadas directamente de la BD
        async with session_scope() as db:
            art = (await db.scalars(
                select(IndexedArt)
                .where(IndexedArt.name_normalized == "sol ring")
                .limit(1)
            )).first()

        if not art:
            pytest.skip("No hay IndexedArt para verificar")

        # Si el source almacenó URLs propias, el search debe usarlas
        if art.thumb_url:
            r = await client.get("/api/drives/search?q=Sol+Ring&limit=100")
            hits = r.json()
            matching = [h for h in hits if h["file_id"] == art.file_id]
            assert len(matching) == 1, f"No encontré el hit para file_id={art.file_id}"
            assert matching[0]["thumb_url"] == art.thumb_url, (
                f"thumb_url no coincide: search devuelve '{matching[0]['thumb_url']}' "
                f"pero IndexedArt tiene '{art.thumb_url}'"
            )

    async def test_gdrive_source_still_generates_drive_urls(self):
        """Para sources gdrive (thumb_url=NULL), se debe seguir generando
        la URL de Google Drive como fallback.
        """
        from mpc_forge.services.gdrive_search import _download_url, _thumb_url

        # Simular un file_id de Google Drive
        gdrive_file_id = "1abc123XYZ_test"
        thumb = _thumb_url(gdrive_file_id)
        download = _download_url(gdrive_file_id)
        assert "drive.google.com" in thumb
        assert gdrive_file_id in thumb
        assert "drive.google.com" in download
        assert gdrive_file_id in download
