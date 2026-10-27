from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflows() -> dict[str, dict]:
    return {
        p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
        for p in sorted(WORKFLOWS.glob("*.yml"))
    }


def _runs_on_windows(job: dict) -> bool:
    if "windows" in str(job.get("runs-on", "")).lower():
        return True
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    values = [*(matrix.get("os") or []), *(e.get("os", "") for e in matrix.get("include") or [])]
    return any("windows" in str(v).lower() for v in values)


def _launcher():
    spec = importlib.util.spec_from_file_location(
        "mpcforge_launcher", ROOT / "packaging" / "launcher.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_uses_the_hashed_universal_lockfile():
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in release
    assert "pip install -r requirements.txt" not in release
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    uvloop = next(ln for ln in lock.splitlines() if ln.startswith("uvloop=="))
    assert "sys_platform" in uvloop, "Regenera el lockfile con `uv pip compile --universal`"


def test_frozen_launcher_arguments(monkeypatch):
    launcher = _launcher()
    args = launcher._parse_args(["--no-browser", "--port", "8791"])
    assert args.port == 8791 and args.no_browser is True
    assert launcher._parse_args([]).port == 8765

    monkeypatch.setenv("MPC_FORGE_PORT", "9000")
    assert launcher._parse_args([]).port == 9000
    assert launcher._parse_args(["--port", "7777"]).port == 7777

    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--no-browser" in release and "--port 8791" in release


def test_the_bundle_keeps_what_the_frozen_app_needs():
    spec = (ROOT / "packaging" / "mpc-forge.spec").read_text(encoding="utf-8")
    excludes = set(re.findall(r'"(scipy\.[a-z_]+|pywt)"', spec))
    assert excludes, "el spec debería recortar scipy"
    assert not excludes & {"scipy.fft", "scipy.fftpack", "scipy.linalg", "scipy.special"}
    vendored = re.search(r"_REQUIRED_ASSETS = \[(.*?)\]", spec, re.S).group(1)
    for asset in re.findall(
        r'src="/static/vendor/([^"?]+)', (ROOT / "templates" / "base.html").read_text()
    ):
        assert f'"{asset}"' in vendored, asset
    for data in ("templates", "static", "locales"):
        assert f'"{data}"' in spec, data
