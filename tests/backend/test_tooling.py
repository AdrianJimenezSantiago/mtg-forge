from __future__ import annotations

import importlib.util
import json
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


def test_workflow_steps_are_named_and_fail_fast_on_windows():
    unnamed, no_shell = [], []
    for name, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run")
                if not run or "\n" not in run.strip():
                    continue
                label = f"{name}::{job_name} → {run.splitlines()[0]!r}"
                if not step.get("name"):
                    unnamed.append(label)
                if _runs_on_windows(job) and "shell" not in step:
                    no_shell.append(label)
    assert not unnamed, "Pasos multilínea sin `name`:\n" + "\n".join(unnamed)
    assert not no_shell, (
        "PowerShell no aborta ante un fallo intermedio; añade `shell: bash`:\n"
        + "\n".join(no_shell)
    )


def test_node_toolchain_matches_package_json():
    spec = (json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("engines") or {})[
        "node"
    ]
    required = int(re.search(r"(\d+)", spec).group(1))
    for name, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                if "setup-node" in str(step.get("uses", "")):
                    declared = str((step.get("with") or {}).get("node-version", ""))
                    assert declared, f"{name}::{job_name}: setup-node sin node-version"
                    assert int(declared.strip("'\"").split(".")[0]) >= required, (name, job_name)
    assert "engine-strict=true" in (ROOT / ".npmrc").read_text(encoding="utf-8")
    for script in sorted((ROOT / "scripts").glob("*.mjs")):
        text = script.read_text(encoding="utf-8").replace(" ", "")
        assert "import.meta.url).pathname" not in text, f"{script.name}: usa fileURLToPath"


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
