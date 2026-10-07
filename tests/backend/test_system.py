from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mpc_forge import config as cfg
from mpc_forge import migrations, ssl_config
from mpc_forge.db import session_scope
from mpc_forge.models import Deck, PrintingCache
from mpc_forge.services.system import storage

ROOT = Path(__file__).resolve().parents[2]


def _sqlite(path: Path, script: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()


async def _migrate(path: Path, **kwargs) -> dict:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        report = await migrations.run(conn, **kwargs)
    await engine.dispose()
    return report


def _query(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


class TestMigrations:
    def test_ladder_is_contiguous_documented_and_non_destructive(self):
        versions = [m.version for m in migrations.MIGRATIONS]
        start = migrations.BASELINE_VERSION + 1
        assert versions == list(range(start, start + len(versions)))
        for m in migrations.MIGRATIONS:
            assert m.description.strip(), m.version
            for stmt in m.statements:
                normalized = " ".join(stmt.lower().split())
                assert "drop table" not in normalized and "drop column" not in normalized
                assert "delete from" not in normalized or "where" in normalized
        for module in ("db.py", "migrations.py"):
            assert ".drop_all(" not in (ROOT / "mpc_forge" / module).read_text(encoding="utf-8")

    async def test_fresh_database_is_stamped_without_a_backup(self, tmp_path):
        backups = []
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.sqlite3'}")
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT)")
            )
            report = await migrations.run(conn, on_backup=lambda: backups.append(1) or "x.zip")
        await engine.dispose()
        assert report["fresh"] is True and report["to_version"] == migrations.LATEST_VERSION
        assert backups == []

    async def test_legacy_database_is_adopted_with_its_data_and_a_backup(self, tmp_path):
        db = tmp_path / "legacy.sqlite3"
        _sqlite(
            db,
            """
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '3');
            CREATE TABLE decks (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(256),
                format VARCHAR(32) DEFAULT 'commander', imported_at DATETIME, updated_at DATETIME);
            INSERT INTO decks (name) VALUES ('Atraxa Superfriends');
            INSERT INTO decks (name) VALUES ('Krenko Goblins');
            """,
        )
        backups = []
        report = await _migrate(db, on_backup=lambda: (backups.append(1), "b.zip")[1])
        assert [r[0] for r in _query(db, "SELECT name FROM decks ORDER BY id")] == [
            "Atraxa Superfriends",
            "Krenko Goblins",
        ]
        version = _query(db, "SELECT value FROM kv_store WHERE key='schema_version'")[0][0]
        assert int(version) == migrations.LATEST_VERSION
        assert "adopt-legacy" in report["applied"] and backups

    async def test_upgrade_applies_every_pending_step_once(self, tmp_path):
        db = tmp_path / "v8.sqlite3"
        _sqlite(
            db,
            f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '{migrations.BASELINE_VERSION}');
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name VARCHAR(256));
            INSERT INTO decks (name) VALUES ('Mi mazo');
            CREATE TABLE local_arts (id INTEGER PRIMARY KEY, sha256 VARCHAR(64), relative_path TEXT);
            """,
        )
        report = await _migrate(db)
        assert report["to_version"] == migrations.LATEST_VERSION
        tables = {r[0] for r in _query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"deck_snapshots", "art_themes", "bulk_sync_state"} <= tables
        assert "thumb_path" in {r[1] for r in _query(db, "PRAGMA table_info(local_arts)")}
        assert _query(db, "SELECT COUNT(*) FROM decks")[0][0] == 1
        assert (await _migrate(db))["applied"] == []

    async def test_newer_database_is_left_alone(self, tmp_path):
        db = tmp_path / "future.sqlite3"
        _sqlite(
            db,
            f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '{migrations.LATEST_VERSION + 5}');
            CREATE TABLE decks (id INTEGER PRIMARY KEY, name VARCHAR(256));
            INSERT INTO decks (name) VALUES ('Del futuro');
            """,
        )
        assert (await _migrate(db))["applied"] == []
        assert _query(db, "SELECT COUNT(*) FROM decks")[0][0] == 1

    async def test_failed_step_rolls_back_and_keeps_the_version(self, tmp_path, monkeypatch):
        db = tmp_path / "broken.sqlite3"
        _sqlite(
            db,
            f"""
            CREATE TABLE kv_store (key VARCHAR(128) PRIMARY KEY, value TEXT);
            INSERT INTO kv_store VALUES ('schema_version', '{migrations.BASELINE_VERSION}');
            """,
        )
        bad = migrations.Migration(
            version=migrations.BASELINE_VERSION + 1,
            description="rota",
            statements=["CREATE TABLE ok_before (id INTEGER PRIMARY KEY)", "ESTO NO ES SQL"],
        )
        monkeypatch.setattr(migrations, "MIGRATIONS", [bad])
        assert (await _migrate(db)).get("failed_at") == bad.version
        version = _query(db, "SELECT value FROM kv_store WHERE key='schema_version'")[0][0]
        tables = {r[0] for r in _query(db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert int(version) == migrations.BASELINE_VERSION and "ok_before" not in tables

        def explode():
            raise OSError("disco lleno")

        assert migrations._safe_backup(explode) is None


async def test_datetimes_are_timezone_aware(client):
    async with session_scope() as db:
        deck = Deck(name="tz-test", format="commander")
        db.add(deck)
        db.add(
            PrintingCache(
                scryfall_id="tz-1",
                oracle_id="o-1",
                name="Tz Card",
                set_code="tst",
                set_name="Test",
                collector_number="1",
                rarity="common",
                fetched_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone(timedelta(hours=2))),
            )
        )
        await db.flush()
        deck_id = deck.id

    async with session_scope() as db:
        stored = await db.get(Deck, deck_id)
        assert stored.imported_at.tzinfo and stored.updated_at.tzinfo
        assert stored.imported_at <= datetime.now(UTC)
        printing = await db.get(PrintingCache, "tz-1")
        assert printing.fetched_at.hour == 12 and printing.fetched_at.tzinfo is not None


class TestSettings:
    async def test_definitions_are_grouped(self, client):
        defs = (await client.get("/api/settings/")).json()["definitions"]
        by_group: dict[str, list[str]] = {}
        for d in defs:
            by_group.setdefault(d["group"], []).append(d["key"])
        assert sorted(by_group) == [
            "Búsqueda avanzada",
            "General",
            "MPC Autofill",
            "Precios y envío",
            "Red y conexión",
            "Ubicación de datos",
        ]
        assert {"preferred_language", "prefer_full_art", "default_cardstock"} <= set(
            by_group["General"]
        )
        by_key = {d["key"]: d for d in defs}
        assert by_key["ssl_insecure"]["type"] == "bool"
        assert by_key["ssl_insecure"]["group"] == "Red y conexión"
        path_defs = [d for d in defs if d["key"].startswith("paths.")]
        assert len(path_defs) == 5
        assert all(
            d["type"] == "path" and d["group"] == "Ubicación de datos" and d["default"] == ""
            for d in path_defs
        )

    async def test_ssl_toggle_reaches_the_runtime(self, client):
        assert ssl_config.ssl_insecure() is False
        await client.put("/api/settings/", json={"values": {"ssl_insecure": True}})
        assert ssl_config.ssl_insecure() is True
        await client.put("/api/settings/", json={"values": {"ssl_insecure": False}})
        assert ssl_config.ssl_insecure() is False


class TestPaths:
    async def test_effective_paths_and_runtime_override(self, client):
        data = (await client.get("/api/settings/paths")).json()
        for key in (
            "install_root",
            "data_dir",
            "db_path",
            "art_dir",
            "custom_art_dir",
            "exports_dir",
            "backups_dir",
            "cardbacks_dir",
        ):
            assert data.get(key), key

        custom_dir = tempfile.mkdtemp(prefix="mtgforge_path_test_")
        assert (
            await client.put("/api/settings/", json={"values": {"paths.art_dir": custom_dir}})
        ).status_code == 200
        assert Path(cfg.PATHS.art_dir).resolve() == Path(custom_dir).resolve()
        served = (await client.get("/api/settings/paths")).json()["art_dir"]
        assert Path(served).resolve() == Path(custom_dir).resolve()
        await client.put("/api/settings/", json={"values": {"paths.art_dir": ""}})

    def test_overrides_only_touch_content_folders(self):
        new_art = Path(tempfile.gettempdir()) / "mtg_new_art"
        paths = cfg.PATHS.with_overrides(art_dir=str(new_art), exports_dir="   ")
        assert paths.data_dir == cfg.PATHS.data_dir and paths.db_path == cfg.PATHS.db_path
        assert Path(paths.art_dir).resolve() == new_art.resolve()
        assert paths.exports_dir == cfg.PATHS.exports_dir
        assert cfg.PATHS.with_overrides(art_dir="/tmp/\x00/invalid").art_dir == cfg.PATHS.art_dir


@pytest.fixture
def disk_content(tmp_path, monkeypatch):
    storage.invalidate()
    p = cfg.PATHS.__class__(
        data_dir=tmp_path,
        db_path=tmp_path / "mpc_forge.sqlite3",
        art_dir=tmp_path / "art",
        custom_art_dir=tmp_path / "custom_art",
        exports_dir=tmp_path / "exports",
        backups_dir=tmp_path / "backups",
        cardbacks_dir=tmp_path / "cardbacks",
        thumbs_dir=tmp_path / "thumbs",
    )
    monkeypatch.setattr(cfg, "PATHS", p)

    def write(path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    write(p.art_dir / "ab" / "cd" / "art.png", 5000)
    write(p.custom_art_dir / "mio.jpg", 400)
    write(p.cardbacks_dir / "back.png", 300)
    write(p.thumbs_dir / "thumb.webp", 150)
    write(p.exports_dir / "deck.pdf", 900)
    for day, suffix, size in ((1, "-pre-migration", 70), (2, "-pre-migration", 80), (3, "", 90)):
        backup = write(p.backups_dir / f"mpc-forge-backup-2025010{day}-010101{suffix}.zip", size)
        mtime = 1_735_693_261 + (day - 1) * 86_400
        os.utime(backup, (mtime, mtime))
    write(p.data_dir / "logs" / "mpc-forge.log.1", 60)
    write(p.data_dir / "tag_vocabulary.json", 25)
    yield p
    storage.invalidate()


class TestStorage:
    def test_breakdown_measures_and_classifies_every_category(self, disk_content):
        snap = storage.compute()
        rows = {c["key"]: c for c in snap["categories"]}
        assert set(rows) == {c.key for c in storage.CATEGORIES}
        assert {k: rows[k]["bytes"] for k in rows if k != "database"} == {
            "art": 5000,
            "custom_art": 400,
            "cardbacks": 300,
            "thumbs": 150,
            "exports": 900,
            "backups": 240,
            "logs": 60,
            "other": 25,
        }
        assert snap["totals"]["bytes"] == sum(c["bytes"] for c in snap["categories"])
        assert snap["totals"]["reclaimable_bytes"] == 150 + 900 + 240 + 60
        assert rows["thumbs"]["purge_target"] == "thumbs"
        assert all(rows[k]["reclaimable"] for k in ("thumbs", "exports", "backups", "logs"))
        assert all(
            not rows[k]["reclaimable"] and rows[k]["purge_target"] is None
            for k in ("database", "custom_art", "cardbacks")
        )
        assert snap["backup_estimate"]["full_bytes"] == sum(
            rows[k]["bytes"] for k in ("database", "art", "custom_art", "cardbacks")
        )
        assert snap["backup_estimate"]["db_only_bytes"] == rows["database"]["bytes"]
        assert (
            snap["backups"]["count"],
            snap["backups"]["automatic"],
            snap["backups"]["manual"],
        ) == (
            3,
            2,
            1,
        )
        assert snap["volumes"] and all(
            v["total_bytes"] > 0 and v["free_bytes"] >= 0 and v["app_bytes"] >= 0
            for v in snap["volumes"]
        )

    def test_database_sidecars_and_nested_folders(self, disk_content, monkeypatch):
        wal = cfg.PATHS.db_path.with_name(cfg.PATHS.db_path.name + "-wal")
        wal.write_bytes(b"x" * 4096)
        db_row = next(c for c in storage.compute()["categories"] if c["key"] == "database")
        assert db_row["bytes"] >= 4096

        nested = cfg.PATHS.art_dir / "exports-anidados"
        nested.mkdir(parents=True)
        (nested / "deck.pdf").write_bytes(b"x" * 1234)
        monkeypatch.setattr(cfg, "PATHS", cfg.PATHS.with_overrides(exports_dir=nested))
        snap = storage.compute()
        sizes = {c["key"]: c["bytes"] for c in snap["categories"]}
        assert sizes["exports"] == 1234 and sizes["art"] == 5000
        assert snap["totals"]["bytes"] == sum(sizes.values())

    def test_survives_missing_folders(self):
        storage.invalidate()
        assert storage.compute()["totals"]["bytes"] >= 0

    def test_purge_only_frees_what_can_be_recovered(self, disk_content):
        for target in ("database", "custom_art", "cardbacks", "art"):
            with pytest.raises(ValueError):
                storage.purge([target])

        before = storage.compute()["totals"]["bytes"]
        storage.peek()
        assert storage.purge(["thumbs"])["freed_bytes"] == 150
        assert not list(cfg.PATHS.thumbs_dir.rglob("*.webp"))
        assert storage.peek() is None
        assert storage.compute()["totals"]["bytes"] == before - 150

        assert storage.purge(["exports"], exports_older_than_days=30)["freed_bytes"] == 0
        assert storage.purge(["exports"], exports_older_than_days=0)["freed_bytes"] == 900

        storage.purge(["backups"], keep_backups=1)
        remaining = sorted(p.name for p in cfg.PATHS.backups_dir.glob("*.zip"))
        assert remaining == [
            "mpc-forge-backup-20250102-010101-pre-migration.zip",
            "mpc-forge-backup-20250103-010101.zip",
        ]

    async def test_endpoints(self, client, disk_content):
        first = (await client.get("/api/storage/")).json()
        assert first["cached"] is False and "reclaimable_bytes" in first["totals"]
        assert {c["key"] for c in first["categories"]} == {c.key for c in storage.CATEGORIES}
        assert (await client.get("/api/storage/")).json()["cached"] is True
        assert (await client.get("/api/storage/?refresh=true")).json()["cached"] is False

        r = await client.post("/api/storage/purge", json={"targets": ["database"]})
        assert r.status_code == 400 and "database" in r.json()["detail"]
        assert (await client.post("/api/storage/purge", json={"targets": []})).status_code == 400

        body = (await client.post("/api/storage/purge", json={"targets": ["thumbs"]})).json()
        assert body["freed_bytes"] == 150
        thumbs = next(c for c in body["storage"]["categories"] if c["key"] == "thumbs")
        assert thumbs["bytes"] == 0
