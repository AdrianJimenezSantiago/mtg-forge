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
import re
from pathlib import Path

log = logging.getLogger(__name__)


_FILE_HANDLER: logging.Handler | None = None
_LOG_PATH: Path | None = None


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(key|api_key|apikey|access_token|token|password|secret)"
                r"=([^&\s\"'<>]+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+|basic\s+|token\s+)?(\S+)"),
     r"\1\2[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED]"),
]


def redact(text: str) -> str:
    """Sustituye credenciales conocidas por ``[REDACTED]``.

    Pública para poder testearla y para usarla en respuestas de error que
    viajan a la UI (no solo en el log).
    """
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecretsFilter(logging.Filter):
    """Filtro de logging que redacta secretos del mensaje ya formateado.

    Se aplica sobre ``record.getMessage()`` (mensaje + args interpolados) y
    también sobre el texto de la excepción, que es justo donde httpx mete la
    URL con la key.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        if record.exc_info:
            import traceback
            record.exc_text = redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        return True


def setup_file_logging(logs_dir: Path) -> Path:
    """Configura el FileHandler global y devuelve la ruta del log activo.

    Idempotente: si ya se llamó, cierra el handler previo antes.
    Rota el log anterior si existe (empezamos limpio cada arranque).
    """
    global _FILE_HANDLER, _LOG_PATH

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "mpc-forge.log"

    if _FILE_HANDLER is not None:
        try:
            logging.getLogger().removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        except Exception:
            pass
        _FILE_HANDLER = None

    if log_path.exists():
        try:
            log_path.unlink()
        except OSError as e:
            log.warning("No se pudo borrar log anterior en %s: %s", log_path, e)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler.addFilter(RedactSecretsFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    for existing in root.handlers:
        if existing is handler:
            continue
        if not any(isinstance(f, RedactSecretsFilter) for f in existing.filters):
            existing.addFilter(RedactSecretsFilter())

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
        except Exception:
            pass
        _FILE_HANDLER = None

    if delete and _LOG_PATH is not None:
        try:
            if _LOG_PATH.exists():
                _LOG_PATH.unlink()
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
        text = _LOG_PATH.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > n_lines:
            lines = lines[-n_lines:]
        return "\n".join(lines)
    except OSError as e:
        return f"[Error leyendo log: {e}]"
