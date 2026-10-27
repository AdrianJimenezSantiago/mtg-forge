"""Actualización automática al arrancar.

Dos modos, según cómo se ejecute la app:

- Binario (PyInstaller): consulta la última release de GitHub. Si es más nueva
  que la versión sellada, descarga el paquete de esta plataforma, lo extrae en
  una carpeta temporal y lanza el ejecutable nuevo con ``--apply-update``. Ese
  proceso espera a que el antiguo termine (en Windows un .exe en marcha no se
  puede sustituir), cambia los archivos de la instalación conservando
  ``user-settings/`` y arranca la versión nueva con los mismos argumentos.
- Código fuente (clon de git): si la rama es ``main`` y no hay cambios locales,
  trae ``origin/main`` con fast-forward, reinstala dependencias si cambió
  ``requirements.txt`` y vuelve a lanzar la app.

Sin conexión, o ante cualquier error, la app arranca con la versión que tiene.
Se desactiva con ``--no-update``, con ``MPC_FORGE_NO_UPDATE=1`` o desde
*Ajustes → General*.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = "AdrianJimenezSantiago/mtg-forge"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"

NO_UPDATE_ENV = "MPC_FORGE_NO_UPDATE"
CLEANUP_ENV = "MPC_FORGE_UPDATE_CLEANUP"
SETTING_KEY = "settings.auto_update"
BACKUP_DIR_NAME = ".update-old"
PORTABLE_DIR_NAME = "user-settings"

CHECK_TIMEOUT = 4.0
GIT_TIMEOUT = 20.0

Version = tuple[int, int, int]


def _say(message: str) -> None:
    print(f"  {message}", flush=True)


def parse_version(text: str | None) -> Version | None:
    if not text:
        return None
    parts = text.strip().lstrip("vV").split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def is_newer(latest: str | None, current: str | None) -> bool:
    new, cur = parse_version(latest), parse_version(current)
    return new is not None and cur is not None and new > cur


def _read_settings(db_path: Path, *keys: str) -> dict[str, str]:
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT key, value FROM kv_store WHERE key IN ({','.join('?' * len(keys))})",
                keys,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {k: (v or "") for k, v in rows}


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def disabled_reason(no_update_flag: bool, db_path: Path | None) -> str | None:
    """Motivo por el que no hay que buscar actualizaciones, o None si sí."""
    if no_update_flag:
        return "--no-update"
    if _truthy(os.environ.get(NO_UPDATE_ENV, "")):
        return NO_UPDATE_ENV
    if db_path is not None:
        stored = _read_settings(db_path, SETTING_KEY).get(SETTING_KEY)
        if stored is not None and not _truthy(stored):
            return "ajuste desactivado"
    return None


def _ssl_verify(db_path: Path | None) -> bool:
    from mpc_forge import ssl_config

    ssl_config.configure_ssl()
    if ssl_config.ssl_insecure():
        return False
    if db_path is not None:
        stored = _read_settings(db_path, "settings.ssl_insecure").get("settings.ssl_insecure")
        if stored is not None and _truthy(stored):
            return False
    return True


@dataclass(frozen=True)
class Release:
    tag: str
    asset_name: str
    asset_url: str
    size: int
    sha256: str | None


def asset_suffix(plat: str | None = None, machine: str | None = None) -> str | None:
    """Sufijo del paquete de la release para esta plataforma (ver release.yml)."""
    plat = plat or sys.platform
    machine = (machine or platform.machine()).lower()
    if plat == "win32":
        return "-windows.zip"
    if plat == "darwin" and machine == "arm64":
        return "-macos-arm64.zip"
    if plat.startswith("linux") and machine in {"x86_64", "amd64"}:
        return "-linux-x86_64.tar.gz"
    return None


def pick_asset(release: dict[str, Any], suffix: str) -> Release | None:
    tag = str(release.get("tag_name") or "")
    if release.get("draft") or release.get("prerelease") or parse_version(tag) is None:
        return None
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        url = asset.get("browser_download_url")
        if name.endswith(suffix) and url:
            digest = str(asset.get("digest") or "")
            return Release(
                tag=tag,
                asset_name=name,
                asset_url=str(url),
                size=int(asset.get("size") or 0),
                sha256=digest.removeprefix("sha256:") if digest.startswith("sha256:") else None,
            )
    return None


def install_target(executable: str | None = None) -> tuple[Path, Path]:
    """Carpeta que se sustituye y ruta del ejecutable dentro de ella.

    En macOS es el bundle ``.app`` entero; en Windows y Linux, la carpeta del
    ejecutable.
    """
    exe = Path(executable or sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent, exe.relative_to(parent)
    return exe.parent, Path(exe.name)


def _download(client: Any, release: Release, dest: Path) -> Path:
    path = dest / release.asset_name
    digest = hashlib.sha256()
    done = 0
    last_shown = -1
    with client.stream("GET", release.asset_url) as resp, path.open("wb") as fh:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or release.size or 0)
        for chunk in resp.iter_bytes(1 << 16):
            fh.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct // 10 != last_shown:
                    last_shown = pct // 10
                    print(f"\r  Descargando {release.tag}… {pct:3d}%", end="", flush=True)
    print()
    if release.sha256 and digest.hexdigest() != release.sha256.lower():
        raise ValueError("la suma SHA-256 de la descarga no coincide")
    return path


def _safe_member(dest: Path, name: str) -> Path:
    target = (dest / name).resolve()
    if target != dest and dest not in target.parents:
        raise ValueError(f"ruta fuera del paquete: {name}")
    return target


def extract(archive: Path, dest: Path) -> None:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                _safe_member(dest, member.name)
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, filter="data")
            else:  # pragma: no cover - Python < 3.11.4
                tar.extractall(dest)  # noqa: S202
        return
    if sys.platform == "darwin":
        subprocess.run(  # noqa: S603
            ["/usr/bin/ditto", "-x", "-k", str(archive), str(dest)],
            check=True,
            capture_output=True,
        )
        return
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            _safe_member(dest, info.filename)
        zf.extractall(dest)  # noqa: S202
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                with contextlib.suppress(OSError):
                    os.chmod(dest / info.filename, mode)


def find_new_root(extracted: Path, exe_rel: Path) -> Path | None:
    """Carpeta del paquete extraído que equivale a la instalación actual."""
    candidates = [extracted, *sorted(p for p in extracted.iterdir() if p.is_dir())]
    for cand in candidates:
        if (cand / exe_rel).is_file():
            return cand
    return None


def _preserved_paths(target: Path) -> list[str]:
    """Datos del usuario que viven dentro de la carpeta que se sustituye."""
    from mpc_forge.paths import install_root

    portable = (install_root() / PORTABLE_DIR_NAME).resolve()
    try:
        return [portable.relative_to(target.resolve()).as_posix()]
    except ValueError:
        return []


def _relaunch_frozen(exe: Path, argv: list[str], env: dict[str, str]) -> None:
    subprocess.Popen([str(exe), *argv], env=env, close_fds=True)  # noqa: S603


def run_frozen_update(argv: list[str], current: str, db_path: Path | None) -> int | None:
    """Descarga e instala la última release. Devuelve un código de salida si la
    app debe cerrarse para dejar paso a la versión nueva."""
    import httpx

    suffix = asset_suffix()
    if suffix is None or parse_version(current) in {None, (0, 0, 0)}:
        return None

    verify = _ssl_verify(db_path)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"MPC-Forge/{current}"}
    try:
        with httpx.Client(timeout=CHECK_TIMEOUT, headers=headers, verify=verify) as client:
            resp = client.get(LATEST_RELEASE_API)
            resp.raise_for_status()
            release = pick_asset(resp.json(), suffix)
    except Exception:
        return None
    if release is None or not is_newer(release.tag, current):
        return None

    _say(f"Nueva versión disponible: {release.tag} (tienes v{current}).")
    target, exe_rel = install_target()
    staging = Path(tempfile.mkdtemp(prefix="mpc-forge-update-"))
    try:
        timeout = httpx.Timeout(30.0, connect=10.0)
        with httpx.Client(
            timeout=timeout, headers=headers, verify=verify, follow_redirects=True
        ) as client:
            archive = _download(client, release, staging)
        extracted = staging / "new"
        extract(archive, extracted)
        archive.unlink(missing_ok=True)
        new_root = find_new_root(extracted, exe_rel)
        if new_root is None:
            raise ValueError("el paquete no contiene el ejecutable esperado")
        plan = {
            "target": str(target),
            "exe_rel": exe_rel.as_posix(),
            "new_root": str(new_root),
            "staging": str(staging),
            "pid": os.getpid(),
            "argv": argv,
            "preserve": _preserved_paths(target),
            "version": release.tag,
        }
        plan_path = staging / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        _relaunch_frozen(new_root / exe_rel, ["--apply-update", str(plan_path)], dict(os.environ))
    except Exception as e:
        print()
        _say(f"No se pudo actualizar ({e}). Se abre la versión actual.")
        shutil.rmtree(staging, ignore_errors=True)
        return None
    _say("Instalando la actualización; la app se abrirá de nuevo en unos segundos…")
    return 0


def _wait_for_exit(pid: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            finally:
                kernel32.CloseHandle(handle)
        return
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def _retry(fn: Any, *args: Any, attempts: int = 50, delay: float = 0.2) -> Any:
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
    return None


def swap_install(target: Path, new_root: Path, preserve: list[str]) -> None:
    """Sustituye en ``target`` las entradas que trae ``new_root``.

    Lo antiguo se aparta a ``target/.update-old`` y se recupera si algo falla.
    Las rutas de ``preserve`` (relativas a ``target``) se devuelven a su sitio
    dentro de la versión nueva.
    """
    backup = target / BACKUP_DIR_NAME
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir()
    entries = sorted(p.name for p in new_root.iterdir())
    moved_out: list[str] = []
    moved_in: list[str] = []
    try:
        for name in entries:
            if (target / name).exists() or (target / name).is_symlink():
                _retry(os.replace, target / name, backup / name)
                moved_out.append(name)
            shutil.move(str(new_root / name), str(target / name))
            moved_in.append(name)
        for rel in preserve:
            old, new = backup / rel, target / rel
            if (old.exists() or old.is_symlink()) and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, new)
    except Exception:
        for rel in preserve:
            old, restored = backup / rel, target / rel
            if not old.exists() and restored.exists():
                with contextlib.suppress(OSError):
                    os.replace(restored, old)
        for name in moved_in:
            path = target / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
        for name in moved_out:
            with contextlib.suppress(OSError):
                os.replace(backup / name, target / name)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def apply_update(plan_path: Path) -> int:
    """Lo ejecuta el binario nuevo (``--apply-update``): sustituye la
    instalación cuando el proceso antiguo ha terminado y arranca la nueva."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    target = Path(plan["target"])
    exe = target / plan["exe_rel"]
    env = dict(os.environ)
    env[NO_UPDATE_ENV] = "1"
    env[CLEANUP_ENV] = plan["staging"]

    _wait_for_exit(int(plan["pid"]))
    try:
        swap_install(target, Path(plan["new_root"]), list(plan.get("preserve") or []))
        _say(f"MPC Forge actualizado a {plan['version']}.")
    except Exception as e:
        _say(f"No se pudo instalar la actualización ({e}). Se abre la versión anterior.")
    _relaunch_frozen(exe, list(plan.get("argv") or []), env)
    return 0


def cleanup_after_update() -> None:
    """Borra la carpeta temporal de la actualización recién instalada."""
    staging = os.environ.pop(CLEANUP_ENV, "")
    if not staging or not Path(staging).name.startswith("mpc-forge-update-"):
        return

    def _cleanup() -> None:
        for _ in range(30):
            shutil.rmtree(staging, ignore_errors=True)
            if not Path(staging).exists():
                return
            time.sleep(1)

    threading.Thread(target=_cleanup, daemon=True).start()


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _relaunch_source(argv: list[str]) -> int:
    env = dict(os.environ)
    env[NO_UPDATE_ENV] = "1"
    try:
        return subprocess.call([sys.executable, "-m", "mpc_forge", *argv], env=env)  # noqa: S603
    except KeyboardInterrupt:
        return 0


def run_source_update(argv: list[str], root: Path) -> int | None:
    """Trae origin/main en un clon de git. Devuelve el código de salida de la
    app relanzada, o None si no hubo nada que actualizar."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main":
            return None
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            return None
        if _git(root, "fetch", "--quiet", "origin", "main").returncode != 0:
            return None
        behind = _git(root, "rev-list", "--count", "HEAD..origin/main").stdout.strip()
        if not behind.isdigit() or int(behind) == 0:
            return None
        if _git(root, "merge-base", "--is-ancestor", "HEAD", "origin/main").returncode != 0:
            _say("Tu rama main tiene commits que no están en origin/main; no se actualiza.")
            return None
        before = _git(root, "rev-parse", "HEAD").stdout.strip()
        if _git(root, "merge", "--ff-only", "--quiet", "origin/main").returncode != 0:
            return None
        changed = _git(root, "diff", "--name-only", before, "HEAD").stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None

    _say(f"Actualizado desde origin/main ({behind} commits nuevos).")
    if "requirements.txt" in changed:
        _say("Instalando dependencias nuevas…")
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")],
            check=False,
        )
        if result.returncode != 0:
            _say("pip falló; revisa las dependencias con: pip install -r requirements.txt")
    return _relaunch_source(argv)


def check_on_startup(argv: list[str], *, no_update: bool = False) -> int | None:
    """Busca e instala actualizaciones antes de arrancar el servidor.

    Devuelve un código de salida si este proceso debe terminar (la versión
    nueva ya se ha lanzado), o None para seguir arrancando con normalidad.
    """
    from mpc_forge import __version__
    from mpc_forge import config as cfg
    from mpc_forge.paths import install_root, is_frozen

    cleanup_after_update()
    db_path = cfg.PATHS.db_path
    if disabled_reason(no_update, db_path):
        return None
    try:
        if is_frozen():
            return run_frozen_update(argv, __version__, db_path)
        return run_source_update(argv, install_root())
    except Exception as e:
        _say(f"No se pudo comprobar si hay actualizaciones ({e}).")
        return None
