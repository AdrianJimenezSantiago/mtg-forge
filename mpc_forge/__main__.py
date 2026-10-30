from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser

import uvicorn

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _open_browser_when_ready(url: str, delay: float = 1.2) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def _access_token(cli_value: str | None) -> str | None:
    from mpc_forge.middleware import ACCESS_TOKEN_ENV

    value = (cli_value or os.environ.get(ACCESS_TOKEN_ENV) or "").strip()
    if value.lower() == "off":
        return None
    return value or secrets.token_urlsafe(18)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mpc-forge", description="Local MTG proxy printing pipeline"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador")
    parser.add_argument(
        "--window",
        action="store_true",
        help="Usar pywebview para abrir en ventana nativa (requiere `pip install pywebview`)",
    )
    parser.add_argument(
        "--no-update", action="store_true", help="No buscar una versión nueva al arrancar"
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Clave de acceso cuando --host no es 127.0.0.1 (por defecto se genera una; "
            "'off' la desactiva)"
        ),
    )
    args = parser.parse_args()

    from mpc_forge import updater

    exit_code = updater.check_on_startup(sys.argv[1:], no_update=args.no_update)
    if exit_code is not None:
        sys.exit(exit_code)

    from mpc_forge.middleware import (
        ACCESS_TOKEN_ENV,
        EXPOSED_ENV,
        NON_LOOPBACK_WARNING,
        is_loopback_bind,
    )

    url = f"http://{args.host}:{args.port}"
    if not is_loopback_bind(args.host):
        log.warning(NON_LOOPBACK_WARNING, args.host)
        token = _access_token(args.token)
        os.environ[EXPOSED_ENV] = "1"
        if token:
            os.environ[ACCESS_TOKEN_ENV] = token
            url = f"{url}/?token={token}"
            print(f"Clave de acceso para la red / network access link: {url}")

    if args.window:
        try:
            import webview
        except ImportError:
            print("pywebview no está instalado. Ejecuta: pip install pywebview")
            print("Cayendo a modo navegador…")
            args.window = False

    if args.window:
        import webview

        def _run_server() -> None:
            uvicorn.run(
                "mpc_forge.app:app",
                host=args.host,
                port=args.port,
                log_level="info",
            )

        t = threading.Thread(target=_run_server, daemon=True)
        t.start()
        time.sleep(1.5)
        webview.create_window("MPC Forge", url, width=1400, height=900)
        webview.start()
    else:
        if not args.no_browser:
            threading.Thread(target=_open_browser_when_ready, args=(url,), daemon=True).start()
        try:
            uvicorn.run(
                "mpc_forge.app:app",
                host=args.host,
                port=args.port,
                log_level="info",
            )
        except KeyboardInterrupt:
            log.info("Cerrando MPC Forge…")


if __name__ == "__main__":
    main()
