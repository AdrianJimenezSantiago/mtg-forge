"""Cuánto ocupa en disco todo lo que la app guarda en local.

Motivación
----------
La app es local-first: la caché de arte de Scryfall, las miniaturas, los PDFs
exportados y los backups crecen sin techo en la carpeta del usuario. Hasta
ahora cada pieza sabía medirse a sí misma (``thumbnails.stats()``,
``backup.list_backups()``) pero no había ningún sitio que respondiera a la
única pregunta que importa: *¿cuánto me está ocupando esto y qué puedo borrar
sin perder nada?*

Este módulo calcula ese desglose y lo clasifica por lo que de verdad decide si
algo se puede borrar:

``essential``    Se pierde para siempre (BD, arte custom, reversos propios).
``refetchable``  Se vuelve a descargar de Scryfall: cuesta tiempo, no datos.
``derived``      Se regenera solo (miniaturas, logs).
``output``       Resultados que ya se entregaron (XML, PDF, ZIP de imágenes).
``safety``       Backups: borrarlos no pierde datos de hoy, sí la red de
                 seguridad de ayer.

Detalles de implementación que no son accidentales
--------------------------------------------------
* El escaneo recorre el disco con ``os.scandir`` y se ejecuta en un hilo
  (:func:`snapshot`), porque con decenas de miles de archivos bloquearía el
  event loop lo suficiente como para congelar la interfaz.
* El resultado se cachea con TTL: la vista de Ajustes hace varias lecturas
  seguidas y el disco no cambia entre ellas.
* Las rutas se leen de ``cfg.PATHS`` en cada llamada, nunca al importar: el
  usuario puede mover ``art_dir`` a otro disco desde Ajustes y el cálculo
  tiene que seguirle.
* Si una carpeta está anidada dentro de otra (p. ej. el usuario apunta
  ``exports_dir`` dentro de ``art_dir``), se excluye del escaneo de la de
  fuera para no contar los mismos bytes dos veces.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mpc_forge import config as cfg

log = logging.getLogger(__name__)

Kind = Literal["essential", "refetchable", "derived", "output", "safety"]

CACHE_TTL_SECONDS = 60.0

PURGE_TARGETS: frozenset[str] = frozenset({"thumbs", "exports", "logs", "backups"})

DEFAULT_KEEP_BACKUPS = 1


@dataclass(frozen=True)
class Category:
    """Una fila del desglose.

    ``purge_target`` es el nombre que hay que pasar a :func:`purge` para
    liberarla, o ``None`` si no se puede borrar desde la app.
    """
    key: str
    kind: Kind
    purge_target: str | None = None


CATEGORIES: tuple[Category, ...] = (
    Category("database", "essential"),
    Category("art", "refetchable"),
    Category("custom_art", "essential"),
    Category("cardbacks", "essential"),
    Category("thumbs", "derived", purge_target="thumbs"),
    Category("exports", "output", purge_target="exports"),
    Category("backups", "safety", purge_target="backups"),
    Category("logs", "derived", purge_target="logs"),
    Category("other", "essential"),
)

_cache: tuple[float, dict[str, Any]] | None = None


def _logs_dir() -> Path:
    """Carpeta de logs. No vive en ``Paths`` porque no es configurable: la fija
    ``app.py`` al arrancar como ``<data_dir>/logs``."""
    return cfg.PATHS.data_dir / "logs"


def category_paths() -> dict[str, Path]:
    """Ruta de cada categoría con los overrides del usuario ya aplicados.

    ``database`` y ``other`` no tienen una carpeta propia (son ficheros
    sueltos dentro de ``data_dir``); se devuelve ``data_dir`` como referencia
    para poder decir al usuario dónde mirar.
    """
    p = cfg.PATHS
    return {
        "database": p.db_path,
        "art": p.art_dir,
        "custom_art": p.custom_art_dir,
        "cardbacks": p.cardbacks_dir,
        "thumbs": p.thumbs_dir,
        "exports": p.exports_dir,
        "backups": p.backups_dir,
        "logs": _logs_dir(),
        "other": p.data_dir,
    }


def _resolved(path: Path) -> Path:
    """``resolve()`` tolerante: una ruta en una unidad desconectada no debe
    tumbar el cálculo entero."""
    try:
        return path.resolve()
    except OSError:
        return path


def _scan_dir(root: Path, *, exclude: frozenset[Path] = frozenset()) -> tuple[int, int]:
    """Recorrido recursivo. Devuelve ``(ficheros, bytes)``.

    Se usa ``os.scandir`` en lugar de ``Path.rglob`` porque devuelve el
    ``stat`` ya cacheado por el sistema operativo: en carpetas de decenas de
    miles de imágenes la diferencia es de un orden de magnitud.

    Los errores se ignoran a propósito y por elemento: un fichero bloqueado
    por el antivirus o un enlace roto no puede invalidar la cifra de todo lo
    demás. No se siguen enlaces simbólicos, que si no un enlace a la raíz del
    disco haría el recorrido infinito.
    """
    files = 0
    total = 0
    stack: list[Path] = [root]
    seen: set[Path] = set()
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            child = _resolved(Path(entry.path))
                            if child in exclude or child in seen:
                                continue
                            seen.add(child)
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, total


def _scan_database() -> tuple[int, int]:
    """La BD son tres ficheros: el ``.sqlite3`` y sus sidecars WAL y SHM.

    El WAL puede tener decenas de MB de transacciones aún sin volcar, así que
    ignorarlo daría una cifra menor que la real justo cuando más ha crecido.
    """
    files = 0
    total = 0
    db = cfg.PATHS.db_path
    for candidate in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        try:
            total += candidate.stat().st_size
            files += 1
        except OSError:
            continue
    return files, total


def _scan_other(exclude: frozenset[Path]) -> tuple[int, int]:
    """Todo lo que cuelga de ``data_dir`` y no cae en ninguna otra categoría.

    Existe para que el total sea de verdad el total: aquí acaban cosas como
    ``tag_vocabulary.json`` (el vocabulario de etiquetas del indexador) o
    cualquier fichero que una versión futura deje ahí sin avisar a este
    módulo. Sin este cajón, el desglose sumaría menos que la carpeta real y
    nadie sabría por qué.
    """
    root = cfg.PATHS.data_dir
    db = cfg.PATHS.db_path
    db_files = {
        _resolved(db),
        _resolved(db.with_name(db.name + "-wal")),
        _resolved(db.with_name(db.name + "-shm")),
    }
    files = 0
    total = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            resolved = _resolved(Path(entry.path))
            if resolved in exclude or resolved in db_files:
                continue
            if entry.is_dir(follow_symlinks=False):
                sub_files, sub_bytes = _scan_dir(Path(entry.path), exclude=exclude)
                files += sub_files
                total += sub_bytes
            elif entry.is_file(follow_symlinks=False):
                files += 1
                total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return files, total


def _mount_point(path: Path) -> Path:
    """Punto de montaje (o letra de unidad en Windows) que contiene ``path``.

    Se sube por el árbol hasta encontrarlo en vez de usar ``path.anchor``
    porque en Linux y macOS un disco externo cuelga de ``/media/...`` o
    ``/Volumes/...`` y el ancla sería siempre ``/``: agruparíamos en un solo
    volumen carpetas que están en discos distintos.
    """
    current = _resolved(path)
    while True:
        try:
            if os.path.ismount(current):
                return current
        except OSError:
            break
        if current.parent == current:
            break
        current = current.parent
    return Path(current.anchor or current)


def _backups_summary() -> dict[str, Any]:
    """Cuántos backups hay, cuáles son automáticos y cuándo fue el último."""
    from mpc_forge.services import backup as backup_service

    try:
        items = backup_service.list_backups(cfg.PATHS.backups_dir)
    except OSError:
        return {"count": 0, "automatic": 0, "manual": 0, "bytes": 0, "latest_at": None}
    automatic = sum(1 for b in items if b["automatic"])
    return {
        "count": len(items),
        "automatic": automatic,
        "manual": len(items) - automatic,
        "bytes": sum(int(b["bytes_size"]) for b in items),
        "latest_at": items[0]["created_at"] if items else None,
    }


def compute() -> dict[str, Any]:
    """Calcula el desglose completo. **Bloquea**: llámalo desde un hilo.

    Usa :func:`snapshot` salvo que sepas lo que haces — esta función no
    cachea y va a disco siempre.
    """
    started = time.monotonic()
    paths = category_paths()
    resolved = {key: _resolved(p) for key, p in paths.items()}

    all_roots = frozenset(resolved.values())

    categories: list[dict[str, Any]] = []
    for cat in CATEGORIES:
        path = paths[cat.key]
        exclude = frozenset(all_roots - {resolved[cat.key]})
        if cat.key == "database":
            files, size = _scan_database()
            exists = cfg.PATHS.db_path.exists()
        elif cat.key == "other":
            files, size = _scan_other(exclude)
            exists = path.exists()
        else:
            exists = path.is_dir()
            files, size = _scan_dir(path, exclude=exclude) if exists else (0, 0)
        categories.append({
            "key": cat.key,
            "kind": cat.kind,
            "path": str(path),
            "exists": exists,
            "files": files,
            "bytes": size,
            "purge_target": cat.purge_target,
            "reclaimable": cat.purge_target is not None,
        })

    total_bytes = sum(c["bytes"] for c in categories)
    by_kind: dict[str, int] = {}
    for c in categories:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + c["bytes"]

    volumes: dict[str, dict[str, Any]] = {}
    for c in categories:
        if not c["exists"]:
            continue
        mount = str(_mount_point(Path(c["path"])))
        vol = volumes.get(mount)
        if vol is None:
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            vol = volumes[mount] = {
                "mount": mount,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "app_bytes": 0,
                "categories": [],
            }
        vol["app_bytes"] += c["bytes"]
        vol["categories"].append(c["key"])

    sizes = {c["key"]: c["bytes"] for c in categories}
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "scan_seconds": round(time.monotonic() - started, 3),
        "data_dir": str(cfg.PATHS.data_dir),
        "categories": categories,
        "totals": {
            "bytes": total_bytes,
            "files": sum(c["files"] for c in categories),
            "reclaimable_bytes": sum(c["bytes"] for c in categories if c["reclaimable"]),
            "by_kind": by_kind,
        },
        "volumes": sorted(volumes.values(), key=lambda v: -v["app_bytes"]),
        "backups": _backups_summary(),
        "backup_estimate": {
            "full_bytes": (
                sizes["database"] + sizes["art"]
                + sizes["custom_art"] + sizes["cardbacks"]
            ),
            "db_only_bytes": sizes["database"],
        },
    }


async def snapshot(*, refresh: bool = False) -> dict[str, Any]:
    """Desglose de almacenamiento, cacheado durante :data:`CACHE_TTL_SECONDS`.

    El cálculo va a un hilo porque recorrer decenas de miles de ficheros
    bloquea el event loop el tiempo suficiente para que la interfaz se note
    congelada mientras tanto.
    """
    global _cache
    if not refresh and _cache is not None:
        age = time.monotonic() - _cache[0]
        if age < CACHE_TTL_SECONDS:
            return {**_cache[1], "cached": True, "age_seconds": round(age, 1)}
    data = await asyncio.to_thread(compute)
    _cache = (time.monotonic(), data)
    return {**data, "cached": False, "age_seconds": 0.0}


def peek() -> dict[str, Any] | None:
    """El snapshot cacheado si lo hay, sin tocar disco. ``None`` si no."""
    if _cache is None:
        return None
    return {**_cache[1], "cached": True}


def invalidate() -> None:
    """Descarta el snapshot cacheado.

    La llaman las operaciones que cambian el disco de forma apreciable (crear
    un backup, vaciar las miniaturas, purgar): sin esto el usuario borraría
    300 MB y la pantalla seguiría enseñando la cifra de antes durante un
    minuto.
    """
    global _cache
    _cache = None


def _delete_files(paths: list[Path]) -> tuple[int, int]:
    """Borra los ficheros indicados. Devuelve ``(borrados, bytes liberados)``."""
    removed = 0
    freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += size
    return removed, freed


def _purge_thumbs() -> tuple[int, int]:
    """Vacía la caché de miniaturas.

    No se delega en ``thumbnails.clear()`` por la misma razón que en los
    backups: ese módulo captura ``PATHS`` al importarse y borraría la carpeta
    por defecto aunque el usuario haya movido las miniaturas a otro disco.
    Aquí se borra la carpeta que la app está usando de verdad.
    """
    directory = cfg.PATHS.thumbs_dir
    if not directory.is_dir():
        return 0, 0
    return _delete_files([f for f in directory.rglob("*.webp") if f.is_file()])


def _purge_exports(older_than_days: int) -> tuple[int, int]:
    """Borra ficheros exportados. ``older_than_days=0`` los borra todos.

    Los exports son PDFs, XMLs y ZIPs que el usuario ya descargó o ya mandó a
    MPC Autofill; la app no los necesita para nada. Aun así el filtro por
    antigüedad existe porque el XML recién generado se abre desde la propia
    interfaz y borrarlo bajo los pies del usuario sería desagradable.
    """
    directory = cfg.PATHS.exports_dir
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - older_than_days * 86400 if older_than_days > 0 else None
    victims = []
    for f in directory.rglob("*"):
        try:
            if not f.is_file():
                continue
            if cutoff is not None and f.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        victims.append(f)
    return _delete_files(victims)


def _purge_logs() -> tuple[int, int]:
    """Borra los logs rotados, nunca el de la sesión en curso.

    El activo lo tiene abierto un ``RotatingFileHandler``: borrarlo en Windows
    falla, y en Linux dejaría al handler escribiendo en un inodo fantasma
    hasta el próximo arranque.
    """
    from mpc_forge.services import logging_setup

    directory = _logs_dir()
    if not directory.is_dir():
        return 0, 0
    active = logging_setup.current_log_path()
    active_resolved = _resolved(active) if active is not None else None
    victims = [
        f for f in directory.iterdir()
        if f.is_file() and _resolved(f) != active_resolved
    ]
    return _delete_files(victims)


def _purge_backups(keep: int) -> tuple[int, int]:
    """Poda los backups automáticos, conservando los ``keep`` más recientes.

    Los backups manuales no se tocan: los creó el usuario a mano y solo él
    sabe cuál le importa.
    """
    from mpc_forge.services import backup as backup_service

    directory = cfg.PATHS.backups_dir
    before = {
        b["path"]: int(b["bytes_size"])
        for b in backup_service.list_backups(directory)
    }
    backup_service.prune_tagged_backups(directory, tag="pre-migration", keep=max(0, keep))
    after = {b["path"] for b in backup_service.list_backups(directory)}
    gone = [p for p in before if p not in after]
    return len(gone), sum(before[p] for p in gone)


def purge(
    targets: list[str] | tuple[str, ...],
    *,
    exports_older_than_days: int = 0,
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
) -> dict[str, Any]:
    """Libera espacio de las categorías indicadas. **Bloquea**.

    Solo acepta objetivos de :data:`PURGE_TARGETS`. La BD, el arte custom y
    los reversos no se pueden borrar desde aquí ni pasando su nombre: son
    datos que no se recuperan de ningún sitio.

    Devuelve el detalle por objetivo y los totales, para poder decir al
    usuario exactamente cuánto se ha liberado.
    """
    unknown = sorted(set(targets) - PURGE_TARGETS)
    if unknown:
        raise ValueError(f"Objetivos no purgables: {', '.join(unknown)}")

    results: dict[str, dict[str, int]] = {}
    for target in ("thumbs", "exports", "logs", "backups"):
        if target not in targets:
            continue
        if target == "thumbs":
            removed, freed = _purge_thumbs()
        elif target == "exports":
            removed, freed = _purge_exports(max(0, exports_older_than_days))
        elif target == "logs":
            removed, freed = _purge_logs()
        else:
            removed, freed = _purge_backups(keep_backups)
        results[target] = {"removed": removed, "freed_bytes": freed}

    invalidate()
    total_freed = sum(r["freed_bytes"] for r in results.values())
    log.info(
        "Limpieza de almacenamiento (%s): %d ficheros, %.1f MB liberados",
        ", ".join(sorted(results)) or "nada", sum(r["removed"] for r in results.values()),
        total_freed / (1024 * 1024),
    )
    return {
        "targets": results,
        "removed": sum(r["removed"] for r in results.values()),
        "freed_bytes": total_freed,
    }
