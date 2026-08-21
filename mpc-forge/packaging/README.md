# Packaging

Contenido de esta carpeta:

- **`launcher.py`** — entry point del ejecutable. Arranca uvicorn y abre el navegador.
- **`mpc-forge.spec`** — spec de PyInstaller (modo `--onedir`).
- **`build.ps1`** — script PowerShell para compilar localmente en Windows.

## Build local

```powershell
# Desde la raíz del repositorio, en un Windows con Python 3.11+
.\packaging\build.ps1
```

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
