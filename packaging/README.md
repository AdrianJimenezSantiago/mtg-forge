# Packaging

Contenido de esta carpeta:

- **`launcher.py`** — entry point del ejecutable. Arranca uvicorn y abre el navegador.
- **`mpc-forge.spec`** — spec de PyInstaller (modo `--onedir`).
- **`build.ps1`** / **`build.bat`** — scripts para compilar localmente en Windows.
- **`make_icon.py`** — genera `icon.ico` (multi-resolución para el `.exe`) y los assets del favicon web.
- **`icon.ico`** — icono del ejecutable Windows (16/32/48/64/128/256 px).
- **`icon-previews/`** — PNGs por resolución para inspeccionar el diseño (no se distribuye).

## Regenerar el icono

Si cambias el diseño en `make_icon.py`:

```bash
python packaging/make_icon.py
```

Genera:
- `packaging/icon.ico` — para el `.exe` (referenciado en `mpc-forge.spec`).
- `static/logo.png` — usado en la sidebar de la app (512×512).
- `static/favicon.png` — favicon del navegador (32×32).

## Build local

Dos formas equivalentes en Windows con Python 3.11+:

- **Doble click en `packaging\build.bat`** — evita advertencias de política de PowerShell.
- **Desde PowerShell**: `.\packaging\build.ps1` (necesita `Unblock-File` la primera vez).

Output: `packaging\dist\MPC-Forge\` — carpeta que contiene `MPC-Forge.exe` y todas
sus dependencias. Para distribuir, comprime esa carpeta entera en un ZIP.

## Build automático (release)

Los pushes de tag `v*` disparan `.github/workflows/release.yml`, que compila en
`windows-latest` y sube el ZIP a la release de GitHub. Ver README principal para
el flujo completo de release.

## Por qué `--onedir` en vez de `--onefile`

- Arranque instantáneo (no descomprime en cada ejecución).
- Menos falsos positivos de antivirus.
- Actualizar es sobreescribir la carpeta.

El único coste es que se distribuye una carpeta con varios archivos en vez de un
solo `.exe`. Como distribuimos siempre un ZIP, no cambia la experiencia del
usuario final.
