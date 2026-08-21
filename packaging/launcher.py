"""Entry point del ejecutable empaquetado con PyInstaller.

Diferencia con `python -m mpc_forge`:
- Localiza recursos con `mpc_forge.paths` (soporta modo frozen).
- Ventana consola muestra un mensaje amable en vez de un traceback si algo va mal.
- Abre el navegador tras un pequeño delay para que el usuario no vea "conexión rechazada".
- Al cerrar consola (Ctrl+C o cerrar ventana), termina uvicorn limpiamente.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    """Abre el navegador tras un pequeño delay en un hilo aparte."""
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_open, daemon=True).start()


def main() -> int:
    # Cabecera amistosa
    print("=" * 60)
    print("  MPC Forge — proxy printing pipeline for MTG")
    print("=" * 60)
    print()

    host = os.environ.get("MPC_FORGE_HOST", "127.0.0.1")
    port = int(os.environ.get("MPC_FORGE_PORT", "8765"))
    url = f"http://{host}:{port}"

    print(f"  Servidor:  {url}")
    print(f"  Datos:     %APPDATA%\\MPC-Forge\\  (Windows)")
    print()
    print("  El navegador se abrirá en unos segundos.")
    print("  Para cerrar la app: cierra esta ventana o pulsa Ctrl+C.")
    print()

    _open_browser_later(url)

    try:
        import uvicorn
        from mpc_forge.app import app
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,   # menos ruido en consola
        )
    except KeyboardInterrupt:
        print("\n  Cerrando MPC Forge...")
        return 0
    except Exception as e:  # noqa: BLE001
        print()
        print("  ✗ Error al arrancar MPC Forge:")
        print(f"    {type(e).__name__}: {e}")
        print()
        print("  Pulsa Enter para cerrar y revisa el log en:")
        print(r"    %APPDATA%\MPC-Forge\logs\mpc-forge.log")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
