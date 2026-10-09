"""Calcula la siguiente versión de la release a partir de los tags y los commits.

Reglas (semver, sin números disparatados como 2.0.600):

- major: algún commit pide «[major]», incluye «BREAKING CHANGE» o usa el
  estilo convencional «tipo!:». Nunca sube solo por acumulación.
- minor: algún commit añade una funcionalidad: «[minor]», «feat:» / «feature:»
  o un asunto que empieza por Add, Implement, Introduce, Support o New.
- patch: todo lo demás (arreglos, refactors, documentación…).
- Un patch nunca pasa de .9: tras x.y.9 la siguiente es x.(y+1).0.
- Si todos los commits llevan «[skip release]», no se publica nada.

Uso:
    python scripts/next_version.py plan [--ref HEAD] [--bump auto|patch|minor|major]
    python scripts/next_version.py stamp 2.7.0
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "mpc_forge" / "__init__.py"

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MAX_PATCH = 9
ORDER = {"patch": 1, "minor": 2, "major": 3}

_MAJOR = re.compile(
    r"\[major\]|BREAKING[ -]CHANGE|^\w+(\([^)]*\))?!:", re.IGNORECASE | re.MULTILINE
)
_MINOR_TAG = re.compile(r"\[minor\]", re.IGNORECASE)
_MINOR_SUBJECT = re.compile(
    r"^(feat|feature)(\([^)]*\))?:|^(add|adds|added|implement|implements|introduce|introduces|"
    r"support|supports|new)\b",
    re.IGNORECASE,
)
_SKIP = re.compile(r"\[(skip|no) release\]", re.IGNORECASE)

Version = tuple[int, int, int]


def parse_tag(tag: str) -> Version | None:
    m = TAG.match(tag.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def fmt(v: Version) -> str:
    return f"{v[0]}.{v[1]}.{v[2]}"


def classify(message: str) -> str | None:
    """Tipo de cambio de un commit, o None si pide no publicar."""
    if _SKIP.search(message):
        return None
    if _MAJOR.search(message):
        return "major"
    subject = message.strip().splitlines()[0] if message.strip() else ""
    if _MINOR_TAG.search(message) or _MINOR_SUBJECT.search(subject):
        return "minor"
    return "patch"


def bump_for(messages: list[str]) -> str | None:
    kinds = [k for k in (classify(m) for m in messages) if k]
    return max(kinds, key=ORDER.__getitem__) if kinds else None


def next_version(current: Version, bump: str) -> Version:
    major, minor, patch = current
    if bump == "major":
        return (major + 1, 0, 0)
    if bump == "minor" or patch + 1 > MAX_PATCH:
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _git(*args: str) -> str:
    # Solo se llama con argumentos fijos de este script.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _versions(tags: str) -> list[tuple[Version, str]]:
    out = []
    for tag in tags.split():
        v = parse_tag(tag)
        if v:
            out.append((v, tag))
    return sorted(out)


def plan(ref: str = "HEAD", forced: str = "auto") -> dict[str, object]:
    sha = _git("rev-parse", ref).strip()
    # Un commit ya incluido en una release (la suya o una posterior, si su CI
    # terminó más tarde que el de un push más nuevo) no genera otra: así los
    # números nunca salen desordenados.
    containing = _versions(_git("tag", "--list", "v*", "--contains", sha))
    if containing:
        return {"release": False, "reason": f"{sha[:7]} ya está en {containing[0][1]}", "sha": sha}

    every = _versions(_git("tag", "--list", "v*"))
    reachable = _versions(_git("tag", "--list", "v*", "--merged", sha))
    current = every[-1][0] if every else (0, 0, 0)
    base = reachable[-1][1] if reachable else None

    log_range = f"{base}..{sha}" if base else sha
    raw = _git("log", "--format=%B%x00", log_range)
    messages = [m.strip() for m in raw.split("\x00") if m.strip()]

    bump = forced if forced != "auto" else bump_for(messages)
    if not messages and forced == "auto":
        return {"release": False, "reason": "no hay commits nuevos", "sha": sha}
    if bump is None:
        return {"release": False, "reason": "todos los commits piden [skip release]", "sha": sha}

    version = fmt(next_version(current, bump))
    return {
        "release": True,
        "sha": sha,
        "previous": f"v{fmt(current)}" if every else None,
        "bump": bump,
        "version": version,
        "tag": f"v{version}",
        "commits": len(messages),
    }


def stamp(version: str, path: Path = VERSION_FILE) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"Versión no válida: {version!r}")
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r'^__version__ = "[^"]*"', f'__version__ = "{version}"', text, flags=re.M)
    if n != 1:
        raise SystemExit(f"No se encontró __version__ en {path}")
    path.write_text(new, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--ref", default="HEAD")
    p.add_argument("--bump", choices=["auto", *ORDER], default="auto")
    s = sub.add_parser("stamp")
    s.add_argument("version")
    args = parser.parse_args(argv)

    if args.cmd == "stamp":
        stamp(args.version)
        print(args.version)
        return 0
    print(json.dumps(plan(args.ref, args.bump)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
