from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _runs_on_windows(job: dict) -> bool:
    runs_on = str(job.get("runs-on", ""))
    if "windows" in runs_on.lower():
        return True
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    values = list(matrix.get("os") or [])
    for entry in matrix.get("include") or []:
        values.append(entry.get("os", ""))
    return any("windows" in str(v).lower() for v in values)


ALL_WORKFLOWS = sorted(p.name for p in WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("workflow", ALL_WORKFLOWS)
def test_multiline_run_on_windows_declares_a_shell(workflow):
    offenders: list[str] = []
    for job_name, job in _load(workflow)["jobs"].items():
        if not _runs_on_windows(job):
            continue
        for step in job.get("steps", []):
            run = step.get("run")
            if not run or "\n" not in run.strip():
                continue
            if "shell" not in step:
                offenders.append(
                    f"{workflow}::{job_name} → {step.get('name') or run.splitlines()[0]!r}"
                )

    assert not offenders, (
        "Bloques `run` multilínea en un runner de Windows sin `shell` explícito:\n  "
        + "\n  ".join(offenders)
        + "\n\nPowerShell no aborta ante el fallo de un comando intermedio, así que "
        "el paso saldría en verde con el trabajo a medias. Añade `shell: bash`."
    )


@pytest.mark.parametrize("workflow", ALL_WORKFLOWS)
def test_every_step_has_a_name_or_is_trivial(workflow):
    unnamed: list[str] = []
    for job_name, job in _load(workflow)["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if run and "\n" in run.strip() and not step.get("name"):
                unnamed.append(f"{workflow}::{job_name} → {run.splitlines()[0]!r}")
    assert not unnamed, (
        "Pasos multilínea sin `name` (la UI los etiqueta con su primera línea, "
        "que puede ser engañosa):\n  " + "\n  ".join(unnamed)
    )


def test_release_installs_from_the_lockfile():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in text
    assert "pip install -r requirements.txt" not in text


def test_lockfile_carries_environment_markers():
    lock = (WORKFLOWS.parent.parent / "requirements.lock").read_text(encoding="utf-8")
    assert "uvloop" in lock, "¿cambió el conjunto de dependencias?"
    uvloop_line = next(ln for ln in lock.splitlines() if ln.startswith("uvloop=="))
    assert "sys_platform" in uvloop_line, (
        "uvloop aparece sin marcador de plataforma: el lockfile se generó para "
        "una sola plataforma. Regenéralo con `uv pip compile --universal`."
    )


class TestNodeVersion:
    def _required_major(self) -> int:
        import json

        pkg = json.loads((WORKFLOWS.parent.parent / "package.json").read_text(encoding="utf-8"))
        spec = (pkg.get("engines") or {}).get("node", "")
        assert spec, "package.json debe declarar engines.node"
        import re

        match = re.search(r"(\d+)", spec)
        assert match, f"No se pudo leer un major de engines.node={spec!r}"
        return int(match.group(1))

    @pytest.mark.parametrize("workflow", ALL_WORKFLOWS)
    def test_setup_node_satisfies_engines(self, workflow):
        required = self._required_major()
        for job_name, job in _load(workflow)["jobs"].items():
            for step in job.get("steps", []):
                if "setup-node" not in str(step.get("uses", "")):
                    continue
                declared = str((step.get("with") or {}).get("node-version", ""))
                assert declared, f"{workflow}::{job_name}: setup-node sin node-version"
                major = int(declared.strip().strip("'\"").split(".")[0])
                assert major >= required, (
                    f"{workflow}::{job_name} usa Node {declared} pero "
                    f"package.json exige >= {required}. npm ci no falla por esto: "
                    f"el error aparece luego en tiempo de ejecución."
                )

    def test_engine_strict_is_enabled(self):
        npmrc = WORKFLOWS.parent.parent / ".npmrc"
        assert npmrc.exists(), "Falta .npmrc con engine-strict=true"
        assert "engine-strict=true" in npmrc.read_text(encoding="utf-8")


class TestNodeScriptsArePortable:
    SCRIPTS = sorted((WORKFLOWS.parent.parent / "scripts").glob("*.mjs"))

    @pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
    def test_no_pathname_on_file_urls(self, script):
        text = script.read_text(encoding="utf-8")
        assert "import.meta.url).pathname" not in text.replace(" ", ""), (
            f"{script.name} usa `.pathname` sobre una file:// URL. En Windows "
            f"eso da '/C:/...' y rompe cualquier path.join posterior. "
            f"Usa `fileURLToPath(new URL(...))`."
        )


class TestFrozenLauncher:
    def _launcher(self):
        import importlib.util

        path = WORKFLOWS.parent.parent / "packaging" / "launcher.py"
        spec = importlib.util.spec_from_file_location("mpcforge_launcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_accepts_port_and_no_browser(self):
        args = self._launcher()._parse_args(["--no-browser", "--port", "8791"])
        assert args.port == 8791
        assert args.no_browser is True

    def test_defaults_match_the_documented_port(self):
        assert self._launcher()._parse_args([]).port == 8765

    def test_env_var_still_works_but_flag_wins(self, monkeypatch):
        monkeypatch.setenv("MPC_FORGE_PORT", "9000")
        launcher = self._launcher()
        assert launcher._parse_args([]).port == 9000
        assert launcher._parse_args(["--port", "7777"]).port == 7777

    def test_smoke_test_uses_flags_the_launcher_understands(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        assert "--no-browser" in text and "--port 8791" in text
        launcher = self._launcher()
        launcher._parse_args(["--no-browser", "--port", "8791"])
