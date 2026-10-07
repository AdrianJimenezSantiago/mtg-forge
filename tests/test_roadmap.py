from __future__ import annotations

import io
from pathlib import Path

import pytest


class TestNormalization:
    def test_asciifold_basic(self):
        from mpc_forge.services.gdrive_indexer import normalize_filename

        assert normalize_filename("Jayā Ballard.png") == "jaya ballard"
        assert normalize_filename("Naïve.png") == "naive"
        assert normalize_filename("Café.png") == "cafe"

    def test_asciifold_ligatures(self):
        from mpc_forge.services.gdrive_indexer import normalize_filename

        assert normalize_filename("Æther Vial.png") == "aether vial"
        assert normalize_filename("Œstrus.png") == "oestrus"
        assert normalize_filename("Straße.png") == "strasse"

    def test_normalize_strips_variants(self):
        from mpc_forge.services.gdrive_indexer import normalize_filename

        assert normalize_filename("Forest (Full Art).png") == "forest"
        assert normalize_filename("Forest - by Chowning.png") == "forest"
        assert normalize_filename("Forest [BACK].png") == "forest"


class TestExtractTags:
    def test_extract_full_art(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, flags = extract_tags("Sol Ring (Full Art).png")
        assert csv == "full_art"
        assert flags["is_full_art"] is True
        assert flags["is_borderless"] is False

    def test_extract_multiple_tags(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, flags = extract_tags("Forest (FA, Retro) [BL].png")
        assert set(csv.split(",")) == {"borderless", "full_art", "retro"}
        assert flags["is_full_art"]
        assert flags["is_borderless"]
        assert flags["is_retro"]

    def test_extract_folder_brackets(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, _ = extract_tags("Forest.png", "Chilli/Amonkhet [Full Art]/sub")
        assert "full_art" in csv

    def test_extract_unknown_tag_ignored(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, _ = extract_tags("Forest (asdf).png")
        assert csv == ""


class TestImportSites:
    def test_resolve_moxfield(self):
        from mpc_forge.clients.import_sites import resolve_site

        assert resolve_site("https://www.moxfield.com/decks/abc").__name__ == "MoxfieldSite"
        assert resolve_site("https://moxfield.com/decks/abc").__name__ == "MoxfieldSite"

    def test_resolve_all_supported(self):
        from mpc_forge.clients.import_sites import resolve_site

        cases = [
            ("https://archidekt.com/decks/12345", "ArchidektSite"),
            ("https://cubecobra.com/cube/list/xyz", "CubeCobraSite"),
            ("https://mtggoldfish.com/deck/123", "MTGGoldfishSite"),
            ("https://scryfall.com/@user/decks/abc", "ScryfallSite"),
            ("https://tappedout.net/mtg-decks/x/", "TappedOutSite"),
        ]
        for url, expected in cases:
            got = resolve_site(url)
            assert got is not None and got.__name__ == expected, f"{url} → {got}"

    def test_resolve_unknown_returns_none(self):
        from mpc_forge.clients.import_sites import resolve_site

        assert resolve_site("https://random.example/whatever") is None
        assert resolve_site("not-a-url") is None
        assert resolve_site("") is None

    def test_supported_sites_endpoint(self):
        from mpc_forge.clients.import_sites import list_supported_sites

        sites = list_supported_sites()
        assert len(sites) == 7
        keys = {s["key"] for s in sites}
        assert keys == {
            "moxfield",
            "archidekt",
            "cubecobra",
            "deckstats",
            "mtggoldfish",
            "scryfall",
            "tappedout",
        }

    def test_moxfield_payload_to_text(self):
        from mpc_forge.clients.import_sites.moxfield import _payload_to_text

        payload = {
            "boards": {
                "commanders": {
                    "cards": {
                        "c1": {"quantity": 1, "card": {"name": "Atraxa", "set": "c21", "cn": "2"}}
                    }
                },
                "mainboard": {
                    "cards": {
                        "m1": {
                            "quantity": 1,
                            "card": {"name": "Sol Ring", "set": "cmm", "cn": "451"},
                        },
                        "m2": {"quantity": 4, "card": {"name": "Forest"}},
                        "m3": {
                            "quantity": 1,
                            "card": {
                                "name": "Zanarkand, Ancient Metropolis // Lasting Fayth",
                                "set": "ffa",
                                "cn": "193",
                            },
                        },
                    }
                },
                "sideboard": {"cards": {}},
            }
        }
        text = _payload_to_text(payload)
        assert "//Commanders" in text
        assert "1 Atraxa (c21) 2" in text
        assert "1 Sol Ring (cmm) 451" in text
        assert "4 Forest" in text
        assert "1 Zanarkand, Ancient Metropolis // Lasting Fayth (ffa) 193" in text
        assert "//Sideboard" not in text

    def test_moxfield_payload_to_text_dfc_no_setcn(self):
        from mpc_forge.clients.import_sites.moxfield import _payload_to_text

        payload = {
            "boards": {
                "mainboard": {
                    "cards": {
                        "m1": {
                            "quantity": 1,
                            "card": {
                                "name": "Delver of Secrets // Insectile Aberration",
                            },
                        },
                    }
                },
            }
        }
        text = _payload_to_text(payload)
        assert "1 Delver of Secrets // Insectile Aberration" in text
        assert "Delver of Secrets // Insectile Aberration (" not in text


class TestSupportedSitesAPI:
    async def test_endpoint_returns_all(self, client):
        r = await client.get("/api/decks/import/supported-sites")
        assert r.status_code == 200
        data = r.json()
        keys = {s["key"] for s in data}
        assert "moxfield" in keys and "archidekt" in keys


class TestCanonicalMetadata:
    def test_canonical_filename(self):
        from mpc_forge.services.gdrive_indexer import extract_canonical

        assert extract_canonical("Opt [DMU 100].png") == ("dmu", "100", "filename")

    def test_canonical_folder(self):
        from mpc_forge.services.gdrive_indexer import extract_canonical

        assert extract_canonical("Forest.png", "[LEA 275] folder/") == ("lea", "275", "folder")

    def test_canonical_ignores_tag_vocab(self):
        from mpc_forge.services.gdrive_indexer import extract_canonical

        assert extract_canonical("X [Full Art].png") == (None, None, "")
        assert extract_canonical("X [Alt Art].png") == (None, None, "")
        assert extract_canonical("X [BACK].png") == (None, None, "")

    def test_canonical_no_tag(self):
        from mpc_forge.services.gdrive_indexer import extract_canonical

        assert extract_canonical("Random file.png") == (None, None, "")

    def test_canonical_collector_number_with_symbols(self):
        from mpc_forge.services.gdrive_indexer import extract_canonical

        assert extract_canonical("X [BIG 10★].png") == ("big", "10★", "filename")
        assert extract_canonical("X [SET 42a].png") == ("set", "42a", "filename")


class TestFTS5:
    async def test_fts5_available_reported(self, client):
        r = await client.get("/api/drives/stats")
        assert r.status_code == 200
        data = r.json()
        assert "fts5_available" in data
        assert isinstance(data["fts5_available"], bool)


class TestSourceTypes:
    def test_registry_contains_all_types(self):
        from mpc_forge.services.source_types import list_registered

        keys = {k for k, _ in list_registered()}
        assert keys == {"gdrive", "gdrive-file", "local-folder", "http-listing", "s3"}

    def test_resolve_returns_class(self):
        from mpc_forge.services.source_types import resolve

        assert resolve("gdrive").__name__ == "GDriveSourceType"
        assert resolve("local-folder").__name__ == "LocalFolderSourceType"
        assert resolve("nonexistent") is None

    def test_local_folder_file_id_roundtrip(self):
        from mpc_forge.services.source_types.local_folder import (
            _decode_relpath,
            _encode_relpath,
        )

        for path in ("simple.png", "sub/dir/file.jpg", "áccéntéd (special) [tag].png"):
            assert _decode_relpath(_encode_relpath(path)) == path

    def test_local_folder_validates_url(self, tmp_path):
        from mpc_forge.services.source_types import resolve

        cls = resolve("local-folder")
        assert cls.validate_url(str(tmp_path)).endswith(tmp_path.name)
        assert cls.validate_url(f"file://{tmp_path}").endswith(tmp_path.name)
        with pytest.raises(ValueError):
            cls.validate_url("/nonexistent/path/xyz")
        with pytest.raises(ValueError):
            cls.validate_url("")

    async def test_local_folder_indexing(self, client, tmp_path):
        (tmp_path / "Sol Ring (Full Art).png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (tmp_path / "Forest [DMU 275].jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "Opt.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

        r = await client.post(
            "/api/art-sources/",
            json={
                "name": "Local test",
                "url": str(tmp_path),
            },
        )
        assert r.status_code in (200, 201), r.text
        src_id = r.json()["id"]
        assert r.json()["source_type"] == "local-folder"

        r = await client.post(f"/api/art-sources/{src_id}/index")
        assert r.status_code in (200, 202), r.text

        r = await client.get("/api/drives/search?q=Forest")
        assert r.status_code == 200
        hits = r.json()
        assert any(h["filename"].startswith("Forest") for h in hits)
        forest = next(h for h in hits if h["filename"].startswith("Forest"))
        assert forest["expansion_code"] == "dmu"
        assert forest["collector_number"] == "275"


class TestPHash:
    def test_is_available_reported(self):
        from mpc_forge.services import phash

        assert isinstance(phash.is_available(), bool)

    def test_hamming_distance(self):
        from mpc_forge.services import phash

        assert phash.hamming_distance("ffffffffffffffff", "ffffffffffffffff") == 0
        assert phash.hamming_distance("ffffffffffffffff", "fffffffffffffffe") == 1
        assert phash.hamming_distance("", "abc") == -1
        assert phash.hamming_distance("bad", "worse") == -1

    def test_compute_from_bytes(self):
        from mpc_forge.services import phash

        if not phash.is_available():
            pytest.skip("Pillow/imagehash no instalados")
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (32, 32), color=(255, 128, 0)).save(buf, format="PNG")
        h = phash.compute_from_bytes(buf.getvalue())
        assert h is not None and len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestPrintRuns:
    def _mock(self, name, qty, back=False):
        from mpc_forge.services.xml_generator import DeckCardResolved

        return DeckCardResolved(
            name=name,
            quantity=qty,
            scryfall_id=f"sf-{name}",
            front_path=Path(f"/t/{name}.png"),
            back_path=Path(f"/t/{name}_back.png") if back else None,
            back_name=f"{name} back" if back else None,
            query=name.lower(),
        )

    def test_empty_returns_no_runs(self):
        from mpc_forge.services.print_runs import split_into_runs

        plan = split_into_runs([])
        assert plan.total_runs == 0 and plan.total_cards == 0

    def test_small_deck_one_run(self):
        from mpc_forge.services.print_runs import split_into_runs

        plan = split_into_runs([self._mock("X", 5)])
        assert plan.total_runs == 1
        assert plan.runs[0].tier_size == 18
        assert plan.runs[0].wasted_slots == 13

    def test_exact_tier(self):
        from mpc_forge.services.print_runs import split_into_runs

        cards = [self._mock(f"C{i}", 1) for i in range(612)]
        plan = split_into_runs(cards)
        assert plan.total_runs == 1
        assert plan.runs[0].tier_size == 612
        assert plan.runs[0].wasted_slots == 0

    def test_split_over_max_tier(self):
        from mpc_forge.services.print_runs import split_into_runs

        cards = [self._mock(f"C{i}", 1) for i in range(700)]
        plan = split_into_runs(cards)
        assert plan.total_runs == 2
        assert plan.total_cards == 700
        assert sum(r.total_cards for r in plan.runs) == 700

    def test_fragment_high_quantity_card(self):
        from mpc_forge.services.print_runs import split_into_runs

        plan = split_into_runs([self._mock("Basic", 800)])
        assert plan.total_runs >= 2
        total = sum(sum(c.quantity for c in r.cards) for r in plan.runs)
        assert total == 800

    def test_back_path_preserved_on_fragment(self):
        from mpc_forge.services.print_runs import split_into_runs

        plan = split_into_runs([self._mock("Delver", 700, back=True)])
        assert plan.total_runs >= 2
        for run in plan.runs:
            for c in run.cards:
                assert c.back_path is not None
                assert c.back_name == "Delver back"

    def test_max_tier_forced(self):
        from mpc_forge.services.print_runs import split_into_runs

        cards = [self._mock(f"C{i}", 1) for i in range(500)]
        plan = split_into_runs(cards, max_tier=108)
        assert all(r.tier_size <= 108 for r in plan.runs)
        assert sum(r.total_cards for r in plan.runs) == 500

    def test_summary_dict_json_serializable(self):
        import json

        from mpc_forge.services.print_runs import split_into_runs, summary_dict

        cards = [self._mock(f"C{i}", 1) for i in range(50)]
        plan = split_into_runs(cards)
        d = summary_dict(plan)
        json.dumps(d)
        assert d["total_runs"] == 1

    async def test_preview_endpoint(self, client, deck):
        r = await client.get(f"/api/decks/{deck['id']}/print-runs/preview")
        assert r.status_code == 200
        data = r.json()
        assert data["total_runs"] >= 1


class TestDFCBulkLookup:
    async def test_bulk_lookup_endpoint(self, client):
        from mpc_forge.db import session_scope
        from mpc_forge.models import DFCPair

        async with session_scope() as s:
            s.add(
                DFCPair(
                    front_name="Delver of Secrets",
                    back_name="Insectile Aberration",
                    kind="transform",
                )
            )
            s.add(
                DFCPair(
                    front_name="Bruna, the Fading Light", back_name="Brisela Top", kind="meld_top"
                )
            )

        r = await client.get(
            "/api/dfc-pairs/lookup?names=Delver of Secrets|Nonexistent|Bruna, the Fading Light"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_queried"] == 3
        assert data["total_matched"] == 2
        assert "Delver of Secrets" in data["found"]
        assert data["found"]["Delver of Secrets"]["back_name"] == "Insectile Aberration"
        assert data["found"]["Bruna, the Fading Light"]["kind"] == "meld_top"
        assert "Nonexistent" not in data["found"]

    async def test_bulk_lookup_case_insensitive(self, client):
        from mpc_forge.db import session_scope
        from mpc_forge.models import DFCPair

        async with session_scope() as s:
            s.add(
                DFCPair(
                    front_name="Delver of Secrets",
                    back_name="Insectile Aberration",
                    kind="transform",
                )
            )
        r = await client.get("/api/dfc-pairs/lookup?names=DELVER of secrets")
        assert r.status_code == 200
        data = r.json()
        assert "DELVER of secrets" in data["found"]

    async def test_bulk_lookup_empty(self, client):
        r = await client.get("/api/dfc-pairs/lookup?names=")
        assert r.status_code == 200
        assert r.json()["total_queried"] == 0


class TestRecommender:
    def test_fold_normalizes(self):
        from mpc_forge.services.recommender import _fold

        assert _fold("Yeong-Hao Han") == _fold("Yeong Hao Han") == "yeong hao han"
        assert _fold("Rebecca Guay") == "rebecca guay"

    def test_pick_best_prefers_regular_recent(self):
        from mpc_forge.services.recommender import _pick_best_printing

        candidates = [
            {"promo": True, "full_art": False, "released_at": "2024-01-01"},
            {"promo": False, "full_art": True, "released_at": "2020-01-01"},
            {"promo": False, "full_art": False, "released_at": "2018-01-01"},
            {"promo": False, "full_art": False, "released_at": "2023-01-01"},
        ]
        best = _pick_best_printing(candidates)
        assert best["released_at"] == "2023-01-01"

    async def test_recommend_endpoint(self, client, deck):
        r = await client.post(
            f"/api/decks/{deck['id']}/recommend-by-artist",
            json={"artist": "Test Artist"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["artist_query"] == "Test Artist"
        assert isinstance(data["matched"], list)
        assert isinstance(data["unmatched_count"], int)
        assert isinstance(data["total_deck_uniques"], int)
        assert data["total_deck_uniques"] >= 1


class TestExtrasBackend:
    async def test_dfc_revert_backs_to_fronts(self, client):
        from mpc_forge.db import session_scope
        from mpc_forge.models import DFCPair

        async with session_scope() as s:
            s.add(
                DFCPair(
                    front_name="Delver of Secrets",
                    back_name="Insectile Aberration",
                    kind="transform",
                )
            )
        r = await client.post(
            "/api/decks/import/text",
            json={
                "name": "DFC revert",
                "text": "1 Insectile Aberration\n1 Sol Ring",
                "format": "commander",
            },
        )
        assert r.status_code in (200, 400)

    async def test_dfc_revert_skips_full_dfc_names(self, client):
        from mpc_forge.db import session_scope
        from mpc_forge.models import DFCPair
        from mpc_forge.services.deck_service import _revert_dfc_backs_to_fronts

        async with session_scope() as s:
            s.add(
                DFCPair(
                    front_name="Zanarkand, Ancient Metropolis",
                    back_name="Lasting Fayth",
                    kind="transform",
                )
            )

        entries = [
            {"name": "Zanarkand, Ancient Metropolis // Lasting Fayth", "quantity": 1},
            {"name": "Lasting Fayth", "quantity": 1},
        ]
        async with session_scope() as s:
            await _revert_dfc_backs_to_fronts(s, entries)

        assert entries[0]["name"] == "Zanarkand, Ancient Metropolis // Lasting Fayth"
        assert "dfc_reverted_from" not in entries[0]
        assert entries[1]["name"] == "Zanarkand, Ancient Metropolis"
        assert entries[1].get("dfc_reverted_from") == "Lasting Fayth"

    def test_parser_state_machine_sideboard(self):
        from mpc_forge.clients.moxfield import parse_plain_decklist

        text = "//Commanders\n1 Atraxa\n//Mainboard\n1 Sol Ring\n//Sideboard\n2 Blood Moon"
        entries = parse_plain_decklist(text)
        by_name = {e["name"]: e for e in entries}
        assert by_name["Atraxa"]["role"] == "commander"
        assert by_name["Sol Ring"]["role"] == "mainboard"
        assert by_name["Blood Moon"]["role"] == "sideboard"

    def test_parser_sb_prefix_mtgo(self):
        from mpc_forge.clients.moxfield import parse_plain_decklist

        text = "1 Sol Ring\nSB: 2 Blood Moon\n1 Lightning Bolt"
        entries = parse_plain_decklist(text)
        by_name = {e["name"]: e for e in entries}
        assert by_name["Sol Ring"]["role"] == "mainboard"
        assert by_name["Blood Moon"]["role"] == "sideboard"
        assert by_name["Lightning Bolt"]["role"] == "mainboard"

    def test_parser_line_starting_with_digit_not_header(self):
        from mpc_forge.clients.moxfield import parse_plain_decklist

        entries = parse_plain_decklist("4 Sideboard")
        assert len(entries) == 1
        assert entries[0]["name"] == "Sideboard"
        assert entries[0]["quantity"] == 4

    def test_deckstats_registered(self):
        from mpc_forge.clients.import_sites import resolve_site

        cls = resolve_site("https://deckstats.net/decks/1234/5678-my-deck")
        assert cls is not None
        assert cls.__name__ == "DeckstatsSite"

    def test_vocab_user_override(self, tmp_path, monkeypatch):
        import json

        from mpc_forge import config as _cfg

        old_paths = _cfg.PATHS
        _cfg.PATHS = old_paths.__class__(**{**vars(old_paths), "data_dir": tmp_path})
        try:
            (tmp_path / "tag_vocabulary.json").write_text(
                json.dumps({"aliases": {"gold_border": ["gold border", "gld"]}})
            )
            from mpc_forge.services.gdrive_indexer import (
                extract_tags,
                reload_tag_vocabulary,
            )

            reload_tag_vocabulary()
            csv, _ = extract_tags("Card (gold border).png")
            assert "gold_border" in csv
        finally:
            _cfg.PATHS = old_paths
            from mpc_forge.services.gdrive_indexer import reload_tag_vocabulary as _r

            _r()

    def test_folder_segment_exact_match(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, flags = extract_tags("Opt.png", "Chilli/Full Art/Opt.png")
        assert "full_art" in csv
        assert flags["is_full_art"]

    def test_folder_segment_partial_not_matched(self):
        from mpc_forge.services.gdrive_indexer import extract_tags

        csv, flags = extract_tags("Opt.png", "Full Art Cards/Opt.png")
        assert "full_art" not in csv
        assert not flags["is_full_art"]

    async def test_fts5_rebuild_endpoint(self, client):
        r = await client.post("/api/drives/rebuild-fts5")
        assert r.status_code in (200, 501)

    async def test_validate_source_gdrive(self, client):
        r = await client.post(
            "/api/art-sources/validate",
            json={
                "url": "https://drive.google.com/drive/folders/abc123",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["detected_type"] == "gdrive"

    async def test_validate_source_invalid(self, client):
        r = await client.post(
            "/api/art-sources/validate",
            json={
                "url": "not-a-real-url",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is False
        assert data["error"]

    async def test_validate_source_empty(self, client):
        r = await client.post("/api/art-sources/validate", json={"url": ""})
        assert r.status_code == 200
        assert r.json()["valid"] is False

    async def test_validate_source_local_folder(self, client, tmp_path):
        r = await client.post(
            "/api/art-sources/validate",
            json={
                "url": str(tmp_path),
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["detected_type"] == "local-folder"

    def test_dp_solver_borderline_case(self):
        from pathlib import Path

        from mpc_forge.services.print_runs import (
            split_into_runs,
            split_into_runs_optimized,
        )
        from mpc_forge.services.xml_generator import DeckCardResolved

        cards = [
            DeckCardResolved(
                name=f"C{i}", quantity=1, scryfall_id=f"x{i}", front_path=Path("/t.png"), query=""
            )
            for i in range(620)
        ]
        greedy = split_into_runs(cards)
        optimized = split_into_runs_optimized(cards)
        assert sum(r.total_cards for r in greedy.runs) == 620
        assert sum(r.total_cards for r in optimized.runs) == 620
        assert optimized.total_wasted_slots <= greedy.total_wasted_slots

    async def test_oracle_artist_cache_persists(self, client, deck):
        from sqlalchemy import select as _sel

        from mpc_forge.db import session_scope
        from mpc_forge.models import OracleArtistCache

        r1 = await client.post(
            f"/api/decks/{deck['id']}/recommend-by-artist",
            json={"artist": "Test Artist"},
        )
        assert r1.status_code == 200

        async with session_scope() as s:
            rows = (await s.scalars(_sel(OracleArtistCache))).all()
        assert len(rows) >= 1

    async def test_recommend_by_style_needs_criterion(self, client, deck):
        r = await client.post(
            f"/api/decks/{deck['id']}/recommend-by-style",
            json={},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["matched"] == []

    async def test_recommend_by_style_endpoint(self, client, deck):
        r = await client.post(
            f"/api/decks/{deck['id']}/recommend-by-style",
            json={"set_code": "c21", "borderless": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert "matched" in data
        assert data["query"]["set_code"] == "c21"

    async def test_canonical_validate_endpoint_empty(self, client):
        r = await client.post("/api/drives/canonical/validate")
        assert r.status_code == 200
        assert r.json() == {"checked": 0, "valid": 0, "invalid": 0, "cache_hits": 0}

    async def test_tag_vocabulary_endpoints(self, client):
        r = await client.post("/api/tag-vocabulary/reload")
        assert r.status_code == 200
        r = await client.get("/api/tag-vocabulary/current")
        assert r.status_code == 200
        vocab = r.json()
        assert "full_art" in vocab
        assert "borderless" in vocab

    async def test_phash_compute_all_endpoint(self, client):
        r = await client.post(
            "/api/drives/phash/compute-all",
            json={
                "limit_per_source": 10,
            },
        )
        assert r.status_code in (200, 501)

    def test_s3_registered(self):
        from mpc_forge.services.source_types import resolve

        cls = resolve("s3")
        assert cls is not None
        assert cls.__name__ == "S3SourceType"

    def test_s3_parses_all_url_forms(self):
        from mpc_forge.services.source_types.s3 import _parse_s3_url

        cases = [
            (
                "s3://my-bucket/prefix/sub",
                ("my-bucket", "prefix/sub", "https://my-bucket.s3.amazonaws.com"),
            ),
            ("s3://my-bucket", ("my-bucket", "", "https://my-bucket.s3.amazonaws.com")),
            (
                "https://mtg-drops.s3.amazonaws.com/",
                ("mtg-drops", "", "https://mtg-drops.s3.amazonaws.com"),
            ),
            (
                "https://mtg-drops.s3.eu-west-1.amazonaws.com/some/prefix",
                ("mtg-drops", "some/prefix", "https://mtg-drops.s3.eu-west-1.amazonaws.com"),
            ),
        ]
        for url, expected in cases:
            got = _parse_s3_url(url)
            assert got == expected, f"{url} → {got}"

    def test_s3_validate_url_canonical(self):
        from mpc_forge.services.source_types import resolve

        cls = resolve("s3")
        assert cls.validate_url("https://foo.s3.amazonaws.com/bar/baz") == "s3://foo/bar/baz"
        assert cls.validate_url("s3://foo") == "s3://foo"

    def test_s3_validate_url_invalid(self):
        from mpc_forge.services.source_types import resolve

        cls = resolve("s3")
        with pytest.raises(ValueError):
            cls.validate_url("not-a-url")
        with pytest.raises(ValueError):
            cls.validate_url("s3://")

    def test_s3_autodetect_in_art_sources(self):
        from mpc_forge.services.art_sources import _detect_source_type

        detected_type, canonical = _detect_source_type("s3://mtg-drops/full-art/")
        assert detected_type == "s3"
        assert canonical == "s3://mtg-drops/full-art"
        detected_type, _ = _detect_source_type("https://foo.s3.amazonaws.com/")
        assert detected_type == "s3"
