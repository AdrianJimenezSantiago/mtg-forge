"""Entry point: `python -m mpc_forge` o `mpc-forge`."""
from __future__ import annotations

import argparse
import logging
import threading
import time
import webbrowser

import uvicorn


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _open_browser_when_ready(url: str, delay: float = 1.2) -> None:
    time.sleep(delay)
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mpc-forge", description="Local MTG proxy printing pipeline")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--no-browser", action="store_true", help="No abrir el navegador")
    parser.add_argument(
        "--window", action="store_true",
        help="Usar pywebview para abrir en ventana nativa (requiere `pip install pywebview`)"
    )
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"

    if args.window:
        try:
            import pywebview  # type: ignore  # noqa: F401
        except ImportError:
            print("pywebview no está instalado. Ejecuta: pip install pywebview")
            print("Cayendo a modo navegador…")
            args.window = False

    if args.window:
        # Levantamos uvicorn en un thread y abrimos ventana pywebview.
        import webview  # type: ignore

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
            logging.info("Cerrando MPC Forge…")


if __name__ == "__main__":
    main()
