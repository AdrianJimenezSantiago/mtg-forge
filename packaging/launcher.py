"""Entry point del ejecutable empaquetado con PyInstaller.

Diferencia con `python -m mpc_forge`:
- Localiza recursos con `mpc_forge.paths` (soporta modo frozen).
- Ventana consola muestra un mensaje amable en vez de un traceback si algo va mal.
- Abre el navegador tras un pequeño delay para que el usuario no vea "conexión rechazada".
- Al cerrar consola (Ctrl+C o cerrar ventana), termina uvicorn limpiamente.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _open_browser_later(url: str, delay: float = 1.5) -> None:
    """Abre el navegador tras un pequeño delay en un hilo aparte."""
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_open, daemon=True).start()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Mismos flags que ``python -m mpc_forge``.

    El ejecutable empaquetado no pasaba por argparse: leía el host y el puerto
    de variables de entorno y nada más. Eso significaba que quien tuviera el
    8765 ocupado no tenía forma documentada de cambiarlo, y que cualquier
    argumento se ignoraba en silencio — incluido el `--port` del smoke test de
    la release, que por eso comprobaba un puerto donde no había nadie
    escuchando.

    Las variables de entorno se mantienen como valor por defecto para no
    romper a quien ya las estuviera usando; los flags tienen prioridad.
    """
    parser = argparse.ArgumentParser(
        prog="MPC-Forge",
        description="MPC Forge — proxy printing pipeline for MTG",
    )
    parser.add_argument(
        "--host", default=os.environ.get("MPC_FORGE_HOST", DEFAULT_HOST),
        help="Interfaz donde escuchar (por defecto 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("MPC_FORGE_PORT", str(DEFAULT_PORT))),
        help=f"Puerto donde escuchar (por defecto {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="No abrir el navegador al arrancar",
    )
    return parser.parse_args(argv)


def _data_dir_hint() -> str:
    """Ruta de datos en el formato de la plataforma actual."""
    if sys.platform == "win32":
        return "%APPDATA%\\MPC-Forge\\"
    if sys.platform == "darwin":
        return "~/Library/Application Support/MPC-Forge/"
    return "~/.local/share/MPC-Forge/"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Cabecera amistosa
    print("=" * 60)
    print("  MPC Forge — proxy printing pipeline for MTG")
    print("=" * 60)
    print()

    host = args.host
    port = args.port
    url = f"http://{host}:{port}"

    print(f"  Servidor:  {url}")
    print(f"  Datos:     {_data_dir_hint()}")
    print()
    if not args.no_browser:
        print("  El navegador se abrirá en unos segundos.")
    print("  Para cerrar la app: cierra esta ventana o pulsa Ctrl+C.")
    print()

    if not args.no_browser:
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
        print("  Revisa el log en:")
        print(f"    {_data_dir_hint()}logs/mpc-forge.log")
        # El `input()` solo tiene sentido si hay una consola interactiva: sirve
        # para que la ventana no se cierre de golpe al hacer doble clic. Sin
        # tty (CI, servicio, salida redirigida) colgaría el proceso hasta el
        # timeout, que es exactamente lo que no quieres cuando algo ya ha
        # fallado.
        if sys.stdin is not None and sys.stdin.isatty():
            print()
            print("  Pulsa Enter para cerrar.")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
