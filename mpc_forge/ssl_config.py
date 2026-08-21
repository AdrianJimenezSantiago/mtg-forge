"""Configuración de SSL/TLS.

Los entornos corporativos suelen tener un proxy que intercepta HTTPS con un
certificado firmado por una CA interna. `certifi` (que usa httpx por defecto)
no la conoce y aparece este error:

    httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: self-signed certificate in certificate chain

La solución limpia es usar los certificados del sistema operativo (donde el
admin corporativo ya ha instalado la CA). `truststore` lo hace en Python 3.10+.

Escape hatch: si la variable de entorno `MPC_FORGE_INSECURE_SSL=1` está puesta,
desactivamos la verificación. Solo úsalo si truststore no basta.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


SSL_INSECURE_ENV = "MPC_FORGE_INSECURE_SSL"


def ssl_insecure() -> bool:
    """True si el usuario ha pedido explícitamente desactivar la verificación SSL."""
    return os.environ.get(SSL_INSECURE_ENV, "").strip() in {"1", "true", "yes"}


def configure_ssl() -> str:
    """Configura el manejo de certificados.

    Devuelve un string descriptivo del modo activo (para loggear al arranque).
    """
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
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo inyectar truststore (%s). Usando certifi.", e)
        return "certifi (fallback)"

    return "OS trust store (Windows/macOS/Linux CAs)"
