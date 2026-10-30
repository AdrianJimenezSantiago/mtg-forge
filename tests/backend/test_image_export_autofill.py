from __future__ import annotations

import dataclasses
import os
import sys
import zipfile

import pytest

from mpc_forge import config as cfg
from mpc_forge.services.printing import image_export, mpc_autofill
from mpc_forge.services.printing.xml_generator import DeckCardResolved


class TestImagesZip:
    def test_names_dfc_backs_collisions_and_missing_files(self, tmp_path):
        a = tmp_path / "a.png"
        a.write_bytes(b"A")
        b = tmp_path / "b.jpg"
        b.write_bytes(b"B")
        back = tmp_path / "back.png"
        back.write_bytes(b"K")
        cardback = tmp_path / "Default.png"
        cardback.write_bytes(b"C")
        cards = [
            DeckCardResolved("Fire // Ice", 1, "fi000001", a, back_path=back),
            DeckCardResolved("Sol Ring", 2, "sr000001", b),
            DeckCardResolved("Sol Ring", 1, "sralt002", a),
            DeckCardResolved("Gone: Card?", 1, "gone0001", tmp_path / "missing.png"),
            DeckCardResolved(
                "Delver", 1, "dv000001", a, back_path=tmp_path / "nope.png", back_name="Aberration"
            ),
        ]
        out = tmp_path / "zip" / "deck.zip"
        result = image_export.build_images_zip(cards, out, "1 Sol Ring\n", cardback)

        names = set(zipfile.ZipFile(out).namelist())
        assert {
            "Fire.png",
            "Ice.png",
            "Sol Ring.jpg",
            "Sol Ring.png",
            "Delver.png",
            "_cardback.png",
            "decklist.txt",
            "README.txt",
        } <= names
        assert result.missing_images == 2 and result.total_dfc_backs == 1
        assert result.included_cardback and result.total_unique_cards == 5
        assert result.size_bytes == out.stat().st_size
        readme = zipfile.ZipFile(out).read("README.txt").decode()
        assert "5 cartas únicas" in readme and "_cardback" in readme

    def test_same_name_different_image_gets_a_suffix(self, tmp_path):
        a = tmp_path / "a.png"
        a.write_bytes(b"A")
        b = tmp_path / "b.png"
        b.write_bytes(b"B")
        cards = [
            DeckCardResolved("Opt", 1, "aaaaaaaa1", a),
            DeckCardResolved("Opt", 1, "bbbbbbbb2", b),
        ]
        out = tmp_path / "o.zip"
        result = image_export.build_images_zip(cards, out, "")
        assert {"Opt.png", "Opt__bbbbbbbb.png"} <= set(zipfile.ZipFile(out).namelist())
        assert not result.included_cardback
        assert "2 cartas únicas" in zipfile.ZipFile(out).read("README.txt").decode()
        single = image_export.build_images_zip(cards[:1], tmp_path / "one.zip", "")
        assert "1 carta única" in zipfile.ZipFile(single.zip_path).read("README.txt").decode()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("A/B:C?", "A_B_C_"),
            ("  spaced   out  ", "spaced out"),
            ("...", "unnamed"),
            ("x" * 200, "x" * 120),
            ("tab\there", "tab_here"),
        ],
    )
    def test_safe_filenames(self, raw, expected):
        assert image_export._safe_filename(raw) == expected


@pytest.fixture
def no_autofill(monkeypatch, tmp_path):
    monkeypatch.setattr(mpc_autofill.shutil, "which", lambda name: None)
    monkeypatch.setattr(cfg, "MPC_AUTOFILL_EXE_PATH", "", raising=False)
    monkeypatch.setattr(cfg, "PATHS", dataclasses.replace(cfg.PATHS, data_dir=tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestAutofill:
    def test_not_found(self, no_autofill):
        assert mpc_autofill.detect() == mpc_autofill.AutofillStatus(False, None, source="not_found")

    def test_user_configured_binary(self, no_autofill, monkeypatch):
        exe = no_autofill / "my-autofill"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setattr(cfg, "MPC_AUTOFILL_EXE_PATH", str(exe), raising=False)
        status = mpc_autofill.detect()
        assert status.available and status.source == "user_config"

    def test_found_in_path_or_search_dirs(self, no_autofill, monkeypatch):
        folder = no_autofill / "mpc-autofill"
        folder.mkdir()
        (folder / "autofill").write_text("x")
        status = mpc_autofill.detect()
        assert status.source == "search" and status.exe_path.endswith("autofill")

        monkeypatch.setattr(mpc_autofill.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert mpc_autofill.detect().source == "path"

    def test_candidate_dirs_include_the_configured_folder_once(self, no_autofill, monkeypatch):
        monkeypatch.setattr(cfg, "MPC_AUTOFILL_EXE_PATH", str(no_autofill), raising=False)
        dirs = mpc_autofill._candidate_dirs()
        assert dirs[0] == no_autofill
        resolved = [d.resolve() for d in dirs if d.exists()]
        assert len(resolved) == len(set(resolved))

    def test_launch(self, no_autofill, monkeypatch):
        xml = no_autofill / "deck.xml"
        with pytest.raises(RuntimeError, match="XML no existe"):
            mpc_autofill.launch(xml)
        xml.write_text("<order/>")
        with pytest.raises(RuntimeError, match="MPC Autofill"):
            mpc_autofill.launch(xml)

        calls = []

        class FakeProc:
            pid = 4321

        def fake_popen(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeProc()

        exe = no_autofill / "autofill"
        exe.write_text("x")
        exe.chmod(0o755)
        monkeypatch.setattr(cfg, "MPC_AUTOFILL_EXE_PATH", str(exe), raising=False)
        monkeypatch.setattr(mpc_autofill.subprocess, "Popen", fake_popen)
        assert mpc_autofill.launch(xml) == 4321
        cmd, kwargs = calls[0]
        assert cmd == [str(exe.resolve()), "--directory", str(xml.resolve().parent)]
        if sys.platform != "win32":
            assert kwargs["start_new_session"] is True

    async def test_endpoints(self, client, no_autofill, monkeypatch):
        status = (await client.get("/api/mpc-autofill/status")).json()
        assert status["available"] is False
        r = await client.post("/api/mpc-autofill/launch", json={"xml_filename": "../x.xml"})
        assert r.status_code in (400, 404)
        missing = await client.post("/api/mpc-autofill/launch", json={"xml_filename": "none.xml"})
        assert missing.status_code == 404 and missing.json()["detail"]["code"] == "xml_not_found"

        (cfg.PATHS.exports_dir / "ok.xml").write_text("<order/>")
        try:
            r = await client.post("/api/mpc-autofill/launch", json={"xml_filename": "ok.xml"})
            assert r.status_code == 400
            assert r.json()["detail"]["code"] == "autofill_launch_failed"
            monkeypatch.setattr(mpc_autofill, "launch", lambda path: 99)
            r = await client.post("/api/mpc-autofill/launch", json={"xml_filename": "ok.xml"})
            assert r.json()["launched"] is True and r.json()["pid"] == 99
        finally:
            os.remove(cfg.PATHS.exports_dir / "ok.xml")
