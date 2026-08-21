"""Log temporal a archivo para debug.

Diseño:
- Se escribe a `<data_dir>/logs/mpc-forge.log` mientras la app está viva.
- Se borra al arrancar limpio (cada sesión empieza con log fresco).
- Se borra en el shutdown limpio (para que en uso normal no queden ficheros).
- Si la app crashea (Ctrl+C forzado, kill, error de arranque), el fichero
  queda intacto — perfecto para diagnóstico post-mortem.
- Captura TODO: root logger, no solo mpc_forge.*. Así aparecen también
  uvicorn, sqlalchemy, httpx, etc.

Además del fichero, seguimos escribiendo a stdout con el StreamHandler que
uvicorn ya monta por defecto.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

log = logging.getLogger(__name__)


# Handler global (guardamos la referencia para poder cerrarlo/borrarlo en shutdown)
_FILE_HANDLER: logging.Handler | None = None
_LOG_PATH: Path | None = None


def setup_file_logging(logs_dir: Path) -> Path:
    """Configura el FileHandler global y devuelve la ruta del log activo.

    Idempotente: si ya se llamó, cierra el handler previo antes.
    Rota el log anterior si existe (empezamos limpio cada arranque).
    """
    global _FILE_HANDLER, _LOG_PATH

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "mpc-forge.log"

    # Cerrar handler anterior si existe (por si esto se llama dos veces)
    if _FILE_HANDLER is not None:
        try:
            logging.getLogger().removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        except Exception:  # noqa: BLE001
            pass
        _FILE_HANDLER = None

    # Borrar log anterior — cada arranque empieza limpio
    if log_path.exists():
        try:
            log_path.unlink()
        except OSError as e:
            log.warning("No se pudo borrar log anterior en %s: %s", log_path, e)

    # RotatingFileHandler con límite de tamaño para no llenar el disco
    # si la app queda encendida días. 10 MB por fichero, hasta 3 backups.
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))

    # Adjuntar al root para capturar todo (uvicorn, sqlalchemy, httpx, ...)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    _FILE_HANDLER = handler
    _LOG_PATH = log_path
    return log_path


def teardown_file_logging(delete: bool = True) -> None:
    """Cierra el handler y opcionalmente borra el log del disco.

    Se llama en el shutdown limpio de la app. Si `delete=False`, deja el log
    en el disco para poder inspeccionarlo (útil si el shutdown lo dispara un
    fallo controlado).
    """
    global _FILE_HANDLER, _LOG_PATH

    if _FILE_HANDLER is not None:
        try:
            logging.getLogger().removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        except Exception:  # noqa: BLE001
            pass
        _FILE_HANDLER = None

    if delete and _LOG_PATH is not None:
        try:
            if _LOG_PATH.exists():
                _LOG_PATH.unlink()
            # También borramos los backups rotados
            for i in range(1, 5):
                backup = _LOG_PATH.with_suffix(_LOG_PATH.suffix + f".{i}")
                if backup.exists():
                    backup.unlink()
        except OSError:
            pass

    _LOG_PATH = None


def current_log_path() -> Path | None:
    """Devuelve la ruta del log actual, o None si no está configurado."""
    return _LOG_PATH


def read_tail(n_lines: int = 200) -> str:
    """Lee las últimas `n_lines` del log. Vacío si no existe."""
    if _LOG_PATH is None or not _LOG_PATH.exists():
        return ""
    try:
        # Enfoque simple: leemos todo y cortamos. El log rota a 10 MB así que OK.
        text = _LOG_PATH.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > n_lines:
            lines = lines[-n_lines:]
        return "\n".join(lines)
    except OSError as e:
        return f"[Error leyendo log: {e}]"
