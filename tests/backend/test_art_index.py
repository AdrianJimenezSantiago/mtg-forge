from __future__ import annotations

import json
import random

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from mpc_forge import config as cfg
from mpc_forge.db import session_scope
from mpc_forge.models import ArtSource, IndexedArt
from mpc_forge.services.indexing import gdrive_search
from mpc_forge.services.indexing.art_sources import _detect_source_type
from mpc_forge.services.indexing.gdrive_indexer import (
    extract_canonical,
    extract_tags,
    normalize_filename,
    reload_tag_vocabulary,
)
from mpc_forge.services.indexing.gdrive_search import (
    _download_url,
    _fts_escape,
    _score_match,
    _thumb_url,
)
from mpc_forge.services.indexing.source_types import list_registered, resolve
from mpc_forge.services.indexing.source_types.local_folder import _decode_relpath, _encode_relpath
from mpc_forge.services.indexing.source_types.s3 import _parse_s3_url

LOCAL_FILES = [
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
    "Sol Ring ~ Fan Art.jpg",
    "Sol Ring (Promo).png",
    "Forest.png",
    "Forest (Full Art).png",
    "Forest [DMU 275].jpg",
    "Command Tower.png",
    "Cursed Sol Ring.png",
]


async def _add_local_source(client, folder, name="Local", wait=True) -> int:
    r = await client.post("/api/art-sources/", json={"name": name, "url": str(folder)})
    assert r.status_code in (200, 201) and r.json()["source_type"] == "local-folder"
    source_id = r.json()["id"]
    result = (
        await client.post(f"/api/art-sources/{source_id}/index?wait={str(wait).lower()}")
    ).json()
    assert result["error"] is None and (result["files_added"] > 0 or not wait)
    return source_id


@pytest_asyncio.fixture
async def local_source(client, tmp_path):
    assert not any(set('<>:"|?*') & set(f) for f in LOCAL_FILES)
    for name in LOCAL_FILES:
        payload = (
            b"\x89PNG\r\n\x1a\n" + name.encode() if name.endswith(".png") else b"\xff\xd8\xff\xe0"
        )
        (tmp_path / name).write_bytes(payload)
    (tmp_path / "Retro Frame").mkdir()
    (tmp_path / "Retro Frame" / "Sol Ring (Old Border).png").write_bytes(b"\x89PNG\r\n\x1a\nold")
    return await _add_local_source(client, tmp_path, "Test Drive Sol Ring")


async def _seed_index(n_sol_ring=0, rare=(), filler=0) -> None:
    rnd = random.Random(5)
    words = [f"w{i}" for i in range(600)]
    names = [f"Sol Ring (Artist {k:03d}).png" for k in range(n_sol_ring)]
    names += [f"{name} (v{k}).png" for name, count in rare for k in range(count)]
    names += [" ".join(rnd.sample(words, 2)) + f" {k}.png" for k in range(filler)]
    async with session_scope() as db:
        src = ArtSource(name="Drive Test", url="https://drive.google.com/drive/folders/test")
        db.add(src)
        await db.flush()
        db.add_all(
            IndexedArt(
                source_id=src.id,
                file_id=f"f{i}",
                filename=filename,
                name_normalized=normalize_filename(filename),
                folder_path="",
                tags="",
            )
            for i, filename in enumerate(names)
        )
        await db.commit()


@pytest.fixture
def fts5(monkeypatch):
    monkeypatch.setattr(gdrive_search, "_fts5_available_cache", None)


class TestFilenameParsing:
    def test_normalization(self):
        cases = {
            "Jayā Ballard.png": "jaya ballard",
            "Jaya Ballard": "jaya ballard",
            "Naïve.png": "naive",
            "Café.png": "cafe",
            "Æther Vial.png": "aether vial",
            "Œstrus.png": "oestrus",
            "Straße.png": "strasse",
            "Forest (Full Art).png": "forest",
            "Forest - by Chowning.png": "forest",
            "Forest [BACK].png": "forest",
            "Sol Ring.png": "sol ring",
            "Sol Ring | Fan Art.jpg": "sol ring",
            "Sol Ring [C21 263].png": "sol ring",
            "Sol Ring (Daubrez Borderless).png": "sol ring",
        }
        assert {k: normalize_filename(k) for k in cases} == cases

    def test_tags(self, monkeypatch, tmp_path):
        csv, flags = extract_tags("Sol Ring (Full Art).png")
        assert csv == "full_art" and flags["is_full_art"] and not flags["is_borderless"]
        csv, flags = extract_tags("Forest (FA, Retro) [BL].png")
        assert set(csv.split(",")) == {"borderless", "full_art", "retro"}
        assert flags["is_full_art"] and flags["is_borderless"] and flags["is_retro"]
        assert "full_art" in extract_tags("Forest.png", "Chilli/Amonkhet [Full Art]/sub")[0]
        assert extract_tags("Forest (asdf).png")[0] == ""
        csv, flags = extract_tags("Opt.png", "Chilli/Full Art/Opt.png")
        assert "full_art" in csv and flags["is_full_art"]
        csv, flags = extract_tags("Opt.png", "Full Art Cards/Opt.png")
        assert "full_art" not in csv and not flags["is_full_art"]

        monkeypatch.setattr(
            cfg, "PATHS", cfg.PATHS.__class__(**{**vars(cfg.PATHS), "data_dir": tmp_path})
        )
        (tmp_path / "tag_vocabulary.json").write_text(
            json.dumps({"aliases": {"gold_border": ["gold border", "gld"]}})
        )
        reload_tag_vocabulary()
        try:
            assert "gold_border" in extract_tags("Card (gold border).png")[0]
        finally:
            monkeypatch.undo()
            reload_tag_vocabulary()

    def test_canonical_set_and_number(self):
        assert extract_canonical("Opt [DMU 100].png") == ("dmu", "100", "filename")
        assert extract_canonical("Forest.png", "[LEA 275] folder/") == ("lea", "275", "folder")
        assert extract_canonical("X [BIG 10★].png") == ("big", "10★", "filename")
        assert extract_canonical("X [SET 42a].png") == ("set", "42a", "filename")
        for name in ("X [Full Art].png", "X [Alt Art].png", "X [BACK].png", "Random file.png"):
            assert extract_canonical(name) == (None, None, ""), name


class TestSourceTypes:
    def test_registry_and_local_folders(self, tmp_path):
        assert {k for k, _ in list_registered()} == {
            "gdrive",
            "gdrive-file",
            "local-folder",
            "http-listing",
            "s3",
        }
        assert resolve("gdrive").__name__ == "GDriveSourceType"
        assert resolve("s3").__name__ == "S3SourceType"
        assert resolve("nonexistent") is None
        for path in ("simple.png", "sub/dir/file.jpg", "áccéntéd (special) [tag].png"):
            assert _decode_relpath(_encode_relpath(path)) == path

        local = resolve("local-folder")
        assert local.validate_url(str(tmp_path)).endswith(tmp_path.name)
        assert local.validate_url(f"file://{tmp_path}").endswith(tmp_path.name)
        for bad in ("/nonexistent/path/xyz", ""):
            with pytest.raises(ValueError):
                local.validate_url(bad)

    def test_s3_urls(self):
        assert _parse_s3_url("s3://my-bucket/prefix/sub") == (
            "my-bucket",
            "prefix/sub",
            "https://my-bucket.s3.amazonaws.com",
        )
        assert _parse_s3_url("s3://my-bucket") == (
            "my-bucket",
            "",
            "https://my-bucket.s3.amazonaws.com",
        )
        assert _parse_s3_url("https://mtg-drops.s3.amazonaws.com/") == (
            "mtg-drops",
            "",
            "https://mtg-drops.s3.amazonaws.com",
        )
        assert _parse_s3_url("https://mtg-drops.s3.eu-west-1.amazonaws.com/some/prefix") == (
            "mtg-drops",
            "some/prefix",
            "https://mtg-drops.s3.eu-west-1.amazonaws.com",
        )
        s3 = resolve("s3")
        assert s3.validate_url("https://foo.s3.amazonaws.com/bar/baz") == "s3://foo/bar/baz"
        assert s3.validate_url("s3://foo") == "s3://foo"
        for bad in ("not-a-url", "s3://"):
            with pytest.raises(ValueError):
                s3.validate_url(bad)
        assert _detect_source_type("s3://mtg-drops/full-art/") == ("s3", "s3://mtg-drops/full-art")
        assert _detect_source_type("https://foo.s3.amazonaws.com/")[0] == "s3"

    async def test_validate_endpoint(self, client, tmp_path):
        async def validate(url):
            return (await client.post("/api/art-sources/validate", json={"url": url})).json()

        gdrive = await validate("https://drive.google.com/drive/folders/abc123")
        assert gdrive["valid"] is True and gdrive["detected_type"] == "gdrive"
        local = await validate(str(tmp_path))
        assert local["valid"] is True and local["detected_type"] == "local-folder"
        invalid = await validate("not-a-real-url")
        assert invalid["valid"] is False and invalid["error"]
        assert (await validate(""))["valid"] is False


class TestIndexingAndSearch:
    async def test_stats_follow_indexing(self, client, tmp_path):
        stats = (await client.get("/api/drives/stats")).json()
        assert (stats["total_files"], stats["sources_indexed"]) == (0, 0)
        assert isinstance(stats["fts5_available"], bool)
        for name in ("Sol Ring (Full Art).png", "Opt.png"):
            (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        await _add_local_source(client, tmp_path, wait=False)
        stats = (await client.get("/api/drives/stats")).json()
        assert stats["total_files"] > 0 and stats["sources_indexed"] >= 1

    async def test_local_folder_search_results(self, client, local_source):
        hits = (await client.get("/api/drives/search?q=Sol+Ring&limit=100")).json()
        exact = [h for h in hits if h["score"] == 100]
        assert len(exact) >= 10
        assert all(h["score"] < 100 for h in hits if "Cursed" in h["filename"])
        assert all(h.get("thumb_url") or h.get("download_url") for h in hits)
        assert all("drive.google.com" not in (h.get("thumb_url") or "") for h in hits)
        assert any(h["is_full_art"] for h in hits)

        full_art = (
            await client.get("/api/drives/search?q=Sol+Ring&limit=100&tags_include=full_art")
        ).json()
        assert full_art and all(h["is_full_art"] for h in full_art)

        forest = next(
            h
            for h in (await client.get("/api/drives/search?q=Forest")).json()
            if h["filename"].startswith("Forest [")
        )
        assert (forest["expansion_code"], forest["collector_number"]) == ("dmu", "275")

        async with session_scope() as db:
            art = (
                await db.scalars(
                    select(IndexedArt).where(IndexedArt.name_normalized == "sol ring").limit(1)
                )
            ).first()
        if art.thumb_url:
            [match] = [h for h in hits if h["file_id"] == art.file_id]
            assert match["thumb_url"] == art.thumb_url

    async def test_fts5_mirror_matches_the_table(self, client, local_source):
        async with session_scope() as db:
            try:
                real = (await db.execute(text("SELECT COUNT(*) FROM indexed_art"))).scalar()
                mirrored = (await db.execute(text("SELECT COUNT(*) FROM indexed_art_fts"))).scalar()
                rows = (
                    await db.execute(
                        text(
                            "SELECT ia.filename, ia.name_normalized, s.name FROM indexed_art_fts "
                            "JOIN indexed_art AS ia ON ia.id = indexed_art_fts.rowid "
                            "JOIN art_sources AS s ON s.id = ia.source_id "
                            'WHERE indexed_art_fts MATCH \'"sol" "ring"*\' LIMIT 20'
                        )
                    )
                ).fetchall()
            except Exception as e:
                pytest.skip(f"FTS5 no disponible: {e}")
        assert mirrored == real
        assert rows and all(r[0] and r[1] and r[2] for r in rows)
        assert len([r for r in rows if r[1] == "sol ring"]) >= 5

    async def test_like_fallback_finds_the_same_cards(self, client, local_source, monkeypatch):
        monkeypatch.setattr(gdrive_search, "_fts5_available_cache", False)
        async with session_scope() as db:
            results = await gdrive_search.search(db, "Sol Ring", limit=100)
        assert len([r for r in results if r.score == 100]) >= 5

    async def test_search_spans_sources_and_filters_by_source(self, client, tmp_path):
        ids = []
        for folder, files in (
            ("drive1", ["Sol Ring.png", "Sol Ring (Full Art).png"]),
            ("drive2", ["Sol Ring (Retro).png", "Sol Ring (Borderless).png"]),
        ):
            (tmp_path / folder).mkdir()
            for name in files:
                (tmp_path / folder / name).write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            ids.append(await _add_local_source(client, tmp_path / folder, folder))
        hits = (await client.get("/api/drives/search?q=Sol+Ring&limit=100")).json()
        assert {h["source_id"] for h in hits} == set(ids)
        filtered = (await client.get(f"/api/drives/search?q=Sol+Ring&source_id={ids[0]}")).json()
        assert filtered and all(h["source_id"] == ids[0] for h in filtered)

    async def test_drive_art_stays_out_of_the_printings_picker(self, client, deck, local_source):
        sol = next(
            c
            for c in (await client.get(f"/api/decks/{deck['id']}")).json()["cards"]
            if c["name"] == "Sol Ring"
        )
        assert sol["custom_arts_available"] == 0
        page = (await client.get(f"/api/decks/{deck['id']}/cards/{sol['id']}/prints")).json()
        assert not [p for p in page["items"] + page["custom"] if p.get("kind") == "drive"]
        hits = (await client.get("/api/drives/search?q=Sol Ring&limit=100")).json()
        assert hits and all("sol ring" in h["filename"].lower() or h["score"] < 100 for h in hits)

    def test_scoring_escaping_and_drive_urls(self):
        assert _score_match("sol ring", "sol ring") == 100
        assert _score_match("sol ring", "cursed sol ring") < 100
        assert _score_match("sol", "sol ring") <= 55
        assert _score_match("bruna the fading light", "bruna the fading light retro") >= 85
        assert _fts_escape("sol ring") == '"sol" "ring"*'
        assert _fts_escape("forest") == '"forest"*'
        assert '"' in _fts_escape('sol "ring"')
        assert _fts_escape("") == _fts_escape("   ") == ""
        for url in (_thumb_url("1abc123XYZ_test"), _download_url("1abc123XYZ_test")):
            assert "drive.google.com" in url and "1abc123XYZ_test" in url


class TestSearchPagination:
    async def test_large_result_sets_page_without_overlap(self, client, fts5, monkeypatch):
        await _seed_index(n_sol_ring=450)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert r.headers["X-Total-Count"] == "450" and "X-Total-Capped" not in r.headers
        assert len(r.json()) == 450

        seen, offset = [], 0
        while True:
            r = await client.get(
                "/api/drives/search", params={"q": "Sol Ring", "limit": 100, "offset": offset}
            )
            assert r.headers["X-Total-Count"] == "450"
            seen += [h["file_id"] for h in r.json()]
            if len(r.json()) < 100:
                break
            offset += 100
        assert len(seen) == len(set(seen)) == 450

        params = {"q": "Sol Ring", "limit": 50, "offset": 50}
        first = [
            h["file_id"] for h in (await client.get("/api/drives/search", params=params)).json()
        ]
        again = [
            h["file_id"] for h in (await client.get("/api/drives/search", params=params)).json()
        ]
        assert first == again

        monkeypatch.setattr(gdrive_search, "_MAX_CANDIDATES", 50)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert (r.headers["X-Total-Capped"], r.headers["X-Total-Count"]) == ("1", "50")

    async def test_like_fallback_paginates_and_caps_are_reported(self, client, monkeypatch):
        monkeypatch.setattr(gdrive_search, "_fts5_available_cache", False)
        await _seed_index(n_sol_ring=420)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert r.headers["X-Total-Count"] == "420" and len(r.json()) == 420

        monkeypatch.setattr(gdrive_search, "_MAX_CANDIDATES", 50)
        r = await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 500})
        assert (r.headers["X-Total-Capped"], r.headers["X-Total-Count"]) == ("1", "50")

    async def test_input_validation(self, client):
        assert (
            await client.get("/api/drives/search", params={"q": "Sol Ring", "limit": 501})
        ).status_code == 422
        assert (
            await client.get("/api/drives/search", params={"q": "Sol Ring", "offset": -1})
        ).status_code == 422
        r = await client.get("/api/drives/search", params={"q": "  "})
        assert r.json() == [] and r.headers["X-Total-Count"] == "0"


class TestSearchRelevance:
    RARE = (
        ("Elesh Norn, Grand Cenobite", 6),
        ("Atraxa, Praetors' Voice", 4),
        ("Thassa's Oracle", 3),
    )

    async def test_rare_long_names_are_found_by_fts_and_like(self, client, fts5):
        await _seed_index(n_sol_ring=50, rare=self.RARE, filler=3000)
        async with session_scope() as db:
            assert await gdrive_search._fts5_available(db), "este test necesita FTS5"
            for name, count in self.RARE:
                results = await gdrive_search.search(db, name, limit=100)
                assert len(results) == count and all(r.score == 100 for r in results), name
            fts = await gdrive_search.search_page(db, "Elesh Norn, Grand Cenobite", limit=100)
            gdrive_search._fts5_available_cache = False
            like = await gdrive_search.search_page(db, "Elesh Norn, Grand Cenobite", limit=100)
        assert fts.total == like.total == 6
        assert {r.file_id for r in fts.results} == {r.file_id for r in like.results}

    async def test_other_cards_stay_out_and_typos_still_match(self, client, fts5):
        async with session_scope() as db:
            src = ArtSource(name="D", url="https://drive.google.com/drive/folders/d")
            db.add(src)
            await db.flush()
            for i, fn in enumerate(
                [
                    "Forest.png",
                    "Forest (Full Art).png",
                    "Forest Warden.png",
                    "Sol Ring.png",
                    "Cursed Sol Ring.png",
                ]
            ):
                db.add(
                    IndexedArt(
                        source_id=src.id,
                        file_id=f"x{i}",
                        filename=fn,
                        name_normalized=normalize_filename(fn),
                        folder_path="",
                        tags="",
                    )
                )
            await db.commit()
            forest = await gdrive_search.search(db, "Forest", limit=100)
            sol = await gdrive_search.search(db, "Sol Ring", limit=100)
            typo = await gdrive_search.search(db, "Sol Rnig", limit=10)
        assert sorted(r.filename for r in forest) == ["Forest (Full Art).png", "Forest.png"]
        assert sol[0].filename == "Sol Ring.png" and all(r.score < 100 for r in sol[1:])
        assert typo


async def test_index_maintenance_endpoints(client):
    assert (await client.post("/api/drives/rebuild-fts5")).status_code in (200, 501)
    assert (await client.post("/api/drives/canonical/validate")).json() == {
        "checked": 0,
        "valid": 0,
        "invalid": 0,
        "cache_hits": 0,
    }
    assert (await client.post("/api/tag-vocabulary/reload")).status_code == 200
    vocab = (await client.get("/api/tag-vocabulary/current")).json()
    assert {"full_art", "borderless"} <= set(vocab)
    r = await client.post("/api/drives/phash/compute-all", json={"limit_per_source": 10})
    assert r.status_code in (200, 501)
