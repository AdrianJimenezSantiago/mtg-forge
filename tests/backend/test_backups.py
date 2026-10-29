from __future__ import annotations

import dataclasses
import io
import sqlite3
import zipfile

import pytest

from mpc_forge import config as cfg
from mpc_forge import migrations
from mpc_forge.services.system import restore


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    paths = dataclasses.replace(
        cfg.PATHS,
        backups_dir=tmp_path / "backups",
        custom_art_dir=tmp_path / "custom_art",
        cardbacks_dir=tmp_path / "cardbacks",
    )
    for d in (paths.backups_dir, paths.custom_art_dir, paths.cardbacks_dir):
        d.mkdir(parents=True)
    monkeypatch.setattr(cfg, "PATHS", paths)
    return paths


async def _make_backup(client) -> str:
    r = await client.post("/api/backup")
    assert r.status_code == 200, r.text
    return r.json()["path"].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


class TestRestore:
    async def test_round_trip_brings_back_decks_art_and_keeps_a_safety_copy(
        self, client, deck, isolated_dirs
    ):
        art = isolated_dirs.custom_art_dir / "Sol Ring.png"
        art.write_bytes(b"\x89PNG fake")
        name = await _make_backup(client)

        listed = (await client.get("/api/backups/")).json()
        assert [b["filename"] for b in listed] == [name]
        assert listed[0]["tag"] == "manual" and listed[0]["automatic"] is False

        summary = (await client.get(f"/api/backups/{name}")).json()
        assert summary["decks"] == 1 and summary["cards"] == 3 and summary["art_files"] == 1
        assert summary["schema_version"] == migrations.LATEST_VERSION

        assert (await client.delete(f"/api/decks/{deck['id']}")).status_code == 204
        art.unlink()
        assert (await client.get("/api/decks/")).json() == []

        r = await client.post(f"/api/backups/{name}/restore")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["restored_art_files"] == 1 and body["summary"]["decks"] == 1
        assert body["safety_backup"].endswith("-pre-restore.zip")

        decks = (await client.get("/api/decks/")).json()
        assert [d["name"] for d in decks] == ["Test Deck"]
        assert art.read_bytes() == b"\x89PNG fake"
        from mpc_forge.services.system import backup

        listed = backup.list_backups(isolated_dirs.backups_dir)
        assert {b["tag"]: b["automatic"] for b in listed} == {"manual": False, "pre-restore": True}

    async def test_upload_validates_and_registers_the_file(self, client, deck, isolated_dirs):
        name = await _make_backup(client)
        payload = (isolated_dirs.backups_dir / name).read_bytes()
        r = await client.post(
            "/api/backups/upload", files={"file": ("mine.zip", payload, "application/zip")}
        )
        assert r.status_code == 200, r.text
        assert r.json()["filename"].endswith("-uploaded.zip") and r.json()["decks"] == 1

        bad = await client.post(
            "/api/backups/upload", files={"file": ("x.zip", b"not a zip", "application/zip")}
        )
        assert bad.status_code == 400 and bad.json()["detail"]["code"] == "backup_invalid"
        assert not list(isolated_dirs.backups_dir.glob("*.part"))

    async def test_download_and_delete(self, client, deck, isolated_dirs):
        name = await _make_backup(client)
        r = await client.get(f"/api/backups/{name}/download")
        assert r.status_code == 200 and r.content[:2] == b"PK"
        assert (await client.delete(f"/api/backups/{name}")).status_code == 204
        assert (await client.get("/api/backups/")).json() == []

    @pytest.mark.parametrize(
        "filename", ["../mpc_forge.sqlite3", "mpc-forge-backup-x.txt", "nope.zip"]
    )
    async def test_unknown_or_unsafe_names_are_refused(self, client, isolated_dirs, filename):
        r = await client.get(f"/api/backups/{filename}")
        assert r.status_code in (404, 405)
        r = await client.post(f"/api/backups/{filename}/restore")
        assert r.status_code in (404, 405)

    async def test_backups_from_a_newer_app_are_refused(self, client, deck, isolated_dirs):
        name = await _make_backup(client)
        path = isolated_dirs.backups_dir / name
        tampered = isolated_dirs.backups_dir / "mpc-forge-backup-20990101-000000-manual.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tampered, "w") as dst:
            for item in src.infolist():
                data = src.read(item)
                if item.filename.startswith("db/"):
                    tmp = isolated_dirs.backups_dir / "db.sqlite3"
                    tmp.write_bytes(data)
                    conn = sqlite3.connect(tmp)
                    conn.execute(
                        "UPDATE kv_store SET value=? WHERE key='schema_version'",
                        (str(migrations.LATEST_VERSION + 1),),
                    )
                    conn.commit()
                    conn.close()
                    data = tmp.read_bytes()
                    tmp.unlink()
                dst.writestr(item, data)
        r = await client.post(f"/api/backups/{tampered.name}/restore")
        assert r.status_code == 400 and r.json()["detail"]["code"] == "backup_too_new"
        assert len((await client.get("/api/decks/")).json()) == 1


class TestArchiveSafety:
    def test_members_outside_the_art_folders_are_ignored(self, isolated_dirs):
        assert restore._safe_art_target("custom_art/../../evil.png") is None
        assert restore._safe_art_target("custom_art/sub\\..\\x.png") is None
        assert restore._safe_art_target("db/mpc_forge.sqlite3") is None
        assert restore._safe_art_target("custom_art/") is None
        target = restore._safe_art_target("cardbacks/Default.png")
        assert target == (isolated_dirs.cardbacks_dir / "Default.png").resolve()

    def test_archives_without_a_database_are_rejected(self, isolated_dirs):
        path = isolated_dirs.backups_dir / "mpc-forge-backup-20240101-000000.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("custom_art/a.png", b"x")
        path.write_bytes(buf.getvalue())
        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect(path)
        assert exc.value.code == "backup_invalid"

    def test_foreign_databases_are_rejected(self, isolated_dirs, tmp_path):
        db = tmp_path / "other.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE things (id INTEGER)")
        conn.commit()
        conn.close()
        path = isolated_dirs.backups_dir / "mpc-forge-backup-20240101-000000.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.write(db, "db/mpc_forge.sqlite3")
        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect(path)
        assert exc.value.code == "backup_invalid"

    def test_corrupt_databases_are_rejected(self, isolated_dirs):
        path = isolated_dirs.backups_dir / "mpc-forge-backup-20240101-000000.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("db/mpc_forge.sqlite3", b"SQLite format 3\x00" + b"\xff" * 4096)
        with pytest.raises(restore.RestoreError) as exc:
            restore.inspect(path)
        assert exc.value.code in {"backup_corrupt", "backup_invalid"}


class TestDeckNotes:
    async def test_notes_are_saved_and_returned(self, client, deck):
        assert deck["notes"] == ""
        r = await client.patch(f"/api/decks/{deck['id']}", json={"notes": "Lent to Ana"})
        assert r.status_code == 200 and r.json()["notes"] == "Lent to Ana"
        assert (await client.get(f"/api/decks/{deck['id']}")).json()["notes"] == "Lent to Ana"
        too_long = await client.patch(f"/api/decks/{deck['id']}", json={"notes": "x" * 20_001})
        assert too_long.status_code == 422


async def test_fresh_databases_no_longer_create_the_unused_inventory_table(client):
    from sqlalchemy import text

    from mpc_forge.db import engine

    async with engine.connect() as conn:
        exists = await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE name='physical_inventory'")
        )
        assert exists.first() is None
