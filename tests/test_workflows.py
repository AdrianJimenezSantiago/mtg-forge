"""Invariantes de los workflows de GitHub Actions.

Por qué existe
--------------
Un `run:` multilínea en un runner de Windows usa PowerShell por defecto, y
PowerShell **no aborta** cuando falla un comando intermedio del bloque: solo
cuenta el código de salida del último. Eso hizo que un `pip install` fallido
se reportara como paso correcto, y el error real apareciera varios pasos
después disfrazado de `ModuleNotFoundError` en pytest.

Es un fallo caro precisamente porque el síntoma aparece lejos de la causa.
Estos tests lo convierten en algo que se detecta al escribir el YAML.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _runs_on_windows(job: dict) -> bool:
    """¿Este job puede ejecutarse en Windows?

    Contempla tanto `runs-on: windows-latest` como las matrices, donde el
    sistema operativo llega por expresión (`${{ matrix.os }}`).
    """
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
            # En los runners de Linux y macOS el shell por defecto ya es bash
            # con `set -e`, así que el problema no se da.
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
    """Un paso multilínea sin nombre se muestra en la UI por su primera línea.

    Es lo que hacía que el paso que instalaba el lockfile apareciera como
    "Run python -m pip install --upgrade pip": el nombre no delataba en
    absoluto lo que realmente estaba haciendo.
    """
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
    """La release debe ser reproducible: lockfile con hashes, no rangos."""
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock" in text
    assert "pip install -r requirements.txt" not in text


def test_lockfile_carries_environment_markers():
    """El lock debe ser universal, no específico de la plataforma que lo generó.

    `pip-compile` aplana las dependencias perdiendo sus marcadores: el lock
    resultante listaba `uvloop` sin condición, y `uvloop` no tiene wheels para
    Windows, así que el runner intentaba compilarlo desde fuente y fallaba.
    """
    lock = (WORKFLOWS.parent.parent / "requirements.lock").read_text(encoding="utf-8")
    assert "uvloop" in lock, "¿cambió el conjunto de dependencias?"
    uvloop_line = next(ln for ln in lock.splitlines() if ln.startswith("uvloop=="))
    assert "sys_platform" in uvloop_line, (
        "uvloop aparece sin marcador de plataforma: el lockfile se generó para "
        "una sola plataforma. Regenéralo con `uv pip compile --universal`."
    )
