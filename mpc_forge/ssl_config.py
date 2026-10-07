from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


SSL_INSECURE_ENV = "MPC_FORGE_INSECURE_SSL"

_runtime_insecure: bool = False


def set_runtime_insecure(value: bool) -> None:
    global _runtime_insecure
    _runtime_insecure = bool(value)


def ssl_insecure() -> bool:
    env_on = os.environ.get(SSL_INSECURE_ENV, "").strip() in {"1", "true", "yes"}
    return env_on or _runtime_insecure


def configure_ssl() -> str:
    if ssl_insecure():
        log.warning(
            "SSL verification DESACTIVADA (%s=1). Solo úsalo en entornos "
            "controlados; el tráfico HTTPS no se verificará.",
            SSL_INSECURE_ENV,
        )
        return "insecure (SSL verification off)"

    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        log.info(
            "truststore no instalado — usando bundle certifi por defecto. "
            "Si estás en una red corporativa, instala: pip install truststore"
        )
        return "certifi (default)"
    except Exception as e:
        log.warning("No se pudo inyectar truststore (%s). Usando certifi.", e)
        return "certifi (fallback)"

    return "OS trust store (Windows/macOS/Linux CAs)"
