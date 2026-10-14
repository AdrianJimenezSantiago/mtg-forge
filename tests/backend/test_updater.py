from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from mpc_forge import updater


def _release(tag="v1.4.0", names=("windows.zip", "macos-arm64.zip", "linux-x86_64.tar.gz")):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": f"MPC-Forge-{tag}-{n}",
                "browser_download_url": f"https://example.invalid/{n}",
                "size": 10,
                "digest": "sha256:abc",
            }
            for n in names
        ],
    }


@pytest.mark.parametrize(
    ("latest", "current", "newer"),
    [
        ("v1.4.0", "1.3.9", True),
        ("v1.10.0", "1.9.0", True),
        ("v1.3.9", "1.3.9", False),
        ("v1.3.0", "1.3.9", False),
        ("nightly", "1.3.9", False),
        ("v1.4.0", "dev", False),
    ],
)
def test_compares_versions_numerically(latest, current, newer):
    assert updater.is_newer(latest, current) is newer


@pytest.mark.parametrize(
    ("plat", "machine", "suffix"),
    [
        ("win32", "AMD64", "-windows.zip"),
        ("darwin", "arm64", "-macos-arm64.zip"),
        ("darwin", "x86_64", None),
        ("linux", "x86_64", "-linux-x86_64.tar.gz"),
        ("linux", "aarch64", None),
    ],
)
def test_asset_suffix_matches_release_packages(plat, machine, suffix):
    assert updater.asset_suffix(plat, machine) == suffix


def test_pick_asset_for_platform():
    asset = updater.pick_asset(_release(), "-linux-x86_64.tar.gz")
    assert asset is not None
    assert asset.tag == "v1.4.0"
    assert asset.asset_name == "MPC-Forge-v1.4.0-linux-x86_64.tar.gz"
    assert asset.sha256 == "abc"
    assert updater.pick_asset(_release(names=("windows.zip",)), "-macos-arm64.zip") is None
    assert updater.pick_asset({**_release(), "prerelease": True}, "-windows.zip") is None


def test_install_target_is_the_app_bundle_on_macos(tmp_path):
    exe = tmp_path / "MPC Forge.app" / "Contents" / "MacOS" / "MPC-Forge"
    exe.parent.mkdir(parents=True)
    exe.touch()
    target, rel = updater.install_target(str(exe))
    assert target == (tmp_path / "MPC Forge.app").resolve()
    assert rel == Path("Contents/MacOS/MPC-Forge")

    win = tmp_path / "MPC-Forge" / "MPC-Forge.exe"
    win.parent.mkdir()
    win.touch()
    assert updater.install_target(str(win)) == (win.parent.resolve(), Path("MPC-Forge.exe"))


def test_disabled_by_flag_env_or_setting(tmp_path, monkeypatch):
    monkeypatch.delenv(updater.NO_UPDATE_ENV, raising=False)
    db = tmp_path / "db.sqlite3"
    assert updater.disabled_reason(False, db) is None
    assert updater.disabled_reason(True, db) == "--no-update"

    monkeypatch.setenv(updater.NO_UPDATE_ENV, "1")
    assert updater.disabled_reason(False, db) == updater.NO_UPDATE_ENV
    monkeypatch.delenv(updater.NO_UPDATE_ENV)

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO kv_store VALUES (?, 'true')", (updater.SETTING_KEY,))
    conn.commit()
    assert updater.disabled_reason(False, db) is None
    conn.execute("UPDATE kv_store SET value = 'false'")
    conn.commit()
    conn.close()
    assert updater.disabled_reason(False, db) == "ajuste desactivado"


def test_setting_is_exposed_in_general():
    from mpc_forge.services.system.settings import DEFINITIONS

    sd = next(d for d in DEFINITIONS if f"settings.{d.key}" == updater.SETTING_KEY)
    assert sd.type == "bool"
    assert sd.default is True
    assert sd.group == "General"


def test_extract_and_find_root_for_each_package_layout(tmp_path):
    # Windows: el zip trae el contenido de la carpeta directamente.
    zpath = tmp_path / "w.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("MPC-Forge.exe", "exe")
        zf.writestr("_internal/base_library.zip", "lib")
    updater.extract(zpath, tmp_path / "w")
    assert updater.find_new_root(tmp_path / "w", Path("MPC-Forge.exe")) == tmp_path / "w"

    # Linux: el tar trae la carpeta MPC-Forge/.
    tpath = tmp_path / "l.tar.gz"
    with tarfile.open(tpath, "w:gz") as tar:
        data = b"elf"
        info = tarfile.TarInfo("MPC-Forge/MPC-Forge")
        info.size = len(data)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))
    updater.extract(tpath, tmp_path / "l")
    root = updater.find_new_root(tmp_path / "l", Path("MPC-Forge"))
    assert root == tmp_path / "l" / "MPC-Forge"
    if sys.platform != "win32":  # Windows no tiene bit de ejecución
        assert (root / "MPC-Forge").stat().st_mode & 0o100


def test_extract_rejects_paths_outside_the_package(tmp_path):
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(ValueError, match="fuera del paquete"):
        updater.extract(zpath, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def _install(root: Path, version: str) -> None:
    (root / "_internal").mkdir(parents=True)
    (root / "MPC-Forge.exe").write_text(version)
    (root / "_internal" / "lib.txt").write_text(version)
    (root / "_internal" / f"only-{version}.txt").write_text(version)


def test_swap_install_replaces_program_and_keeps_user_data(tmp_path):
    target, new = tmp_path / "app", tmp_path / "new"
    _install(target, "old")
    _install(new, "new")
    (target / "user-settings").mkdir()
    (target / "user-settings" / "mpc_forge.sqlite3").write_text("decks")

    updater.swap_install(target, new, ["user-settings"])

    assert (target / "MPC-Forge.exe").read_text() == "new"
    assert (target / "_internal" / "lib.txt").read_text() == "new"
    assert not (target / "_internal" / "only-old.txt").exists()
    assert (target / "user-settings" / "mpc_forge.sqlite3").read_text() == "decks"
    assert not (target / updater.BACKUP_DIR_NAME).exists()


def test_swap_install_carries_user_data_nested_in_a_bundle(tmp_path):
    target, new = tmp_path / "MPC Forge.app", tmp_path / "new"
    (target / "Contents" / "MacOS" / "user-settings").mkdir(parents=True)
    (target / "Contents" / "MacOS" / "MPC-Forge").write_text("old")
    (target / "Contents" / "MacOS" / "user-settings" / "db").write_text("decks")
    (new / "Contents" / "MacOS").mkdir(parents=True)
    (new / "Contents" / "MacOS" / "MPC-Forge").write_text("new")

    updater.swap_install(target, new, ["Contents/MacOS/user-settings"])

    assert (target / "Contents" / "MacOS" / "MPC-Forge").read_text() == "new"
    assert (target / "Contents" / "MacOS" / "user-settings" / "db").read_text() == "decks"


def test_swap_install_rolls_back_on_failure(tmp_path, monkeypatch):
    target, new = tmp_path / "app", tmp_path / "new"
    _install(target, "old")
    _install(new, "new")
    real_move = shutil.move

    def flaky_move(src, dst):
        if Path(src).name == "_internal":
            raise OSError("disco lleno")
        return real_move(src, dst)

    monkeypatch.setattr(updater.shutil, "move", flaky_move)
    with pytest.raises(OSError, match="disco lleno"):
        updater.swap_install(target, new, ["user-settings"])

    assert (target / "MPC-Forge.exe").read_text() == "old"
    assert (target / "_internal" / "lib.txt").read_text() == "old"


def test_apply_update_swaps_and_relaunches(tmp_path, monkeypatch):
    target, new = tmp_path / "app", tmp_path / "mpc-forge-update-x" / "new"
    _install(target, "old")
    _install(new, "new")
    plan = {
        "target": str(target),
        "exe_rel": "MPC-Forge.exe",
        "new_root": str(new),
        "staging": str(new.parent),
        "pid": 999_999_999,
        "argv": ["--port", "9000"],
        "preserve": ["user-settings"],
        "version": "v1.4.0",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    launched = {}
    monkeypatch.setattr(updater, "_wait_for_exit", lambda pid: None)
    monkeypatch.setattr(
        updater,
        "_relaunch_frozen",
        lambda exe, argv, env: launched.update(exe=exe, argv=argv, env=env),
    )

    assert updater.apply_update(plan_path) == 0
    assert (target / "MPC-Forge.exe").read_text() == "new"
    assert launched["exe"] == target / "MPC-Forge.exe"
    assert launched["argv"] == ["--port", "9000"]
    assert launched["env"][updater.NO_UPDATE_ENV] == "1"
    assert launched["env"][updater.CLEANUP_ENV] == str(new.parent)


def test_frozen_update_does_nothing_offline(monkeypatch):
    import httpx

    def offline(self, *a, **kw):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(updater, "asset_suffix", lambda: "-windows.zip")
    monkeypatch.setattr(httpx.Client, "get", offline)
    assert updater.run_frozen_update([], "1.3.0", None) is None


def test_dev_builds_never_update(monkeypatch):
    monkeypatch.setattr(updater, "asset_suffix", lambda: "-windows.zip")
    assert updater.run_frozen_update([], "0.0.0", None) is None


# --- Desde un clon de git ---------------------------------------------------

git = shutil.which("git")


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    if git is None:
        pytest.skip("git no disponible")
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _run(tmp_path, "init", "-b", "main", str(seed))
    for repo in (seed,):
        _run(repo, "config", "user.email", "t@example.com")
        _run(repo, "config", "user.name", "t")
    (seed / "app.txt").write_text("v1")
    _run(seed, "add", ".")
    _run(seed, "commit", "-m", "v1")
    _run(seed, "push", str(origin), "main")
    work = tmp_path / "work"
    _run(tmp_path, "clone", str(origin), str(work))

    def publish(text: str) -> None:
        (seed / "app.txt").write_text(text)
        _run(seed, "commit", "-am", text)
        _run(seed, "push", str(origin), "main")

    return work, publish


def test_source_update_fast_forwards_main_and_relaunches(clone, monkeypatch):
    work, publish = clone
    publish("v2")
    relaunched = []
    monkeypatch.setattr(updater, "_relaunch_source", lambda argv: relaunched.append(argv) or 7)

    assert updater.run_source_update(["--no-browser"], work) == 7
    assert (work / "app.txt").read_text() == "v2"
    assert relaunched == [["--no-browser"]]


def test_source_update_is_a_no_op_when_up_to_date(clone, monkeypatch):
    work, _ = clone
    monkeypatch.setattr(updater, "_relaunch_source", lambda argv: pytest.fail("no debe relanzar"))
    assert updater.run_source_update([], work) is None


def test_source_update_leaves_local_changes_and_other_branches_alone(clone, monkeypatch):
    work, publish = clone
    publish("v2")
    monkeypatch.setattr(updater, "_relaunch_source", lambda argv: pytest.fail("no debe relanzar"))

    (work / "app.txt").write_text("mío")
    assert updater.run_source_update([], work) is None
    assert (work / "app.txt").read_text() == "mío"

    _run(work, "checkout", "--", "app.txt")
    _run(work, "checkout", "-b", "feature")
    assert updater.run_source_update([], work) is None
    assert (work / "app.txt").read_text() == "v1"
