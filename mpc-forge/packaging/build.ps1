# Compila MPC Forge a un .exe distribuible (modo --onedir).
# Uso desde la raiz del proyecto:
#   cd C:\ruta\a\mtg-forge
#   .\packaging\build.ps1
#
# Requiere Python 3.11+ y las deps del requirements.txt instaladas.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "=== MPC Forge - build local ===" -ForegroundColor Cyan
Write-Host "Working dir: $repoRoot"
Write-Host ""

# 1. Comprobar Python
$py = python --version 2>&1
Write-Host "Python: $py"

# 2. Instalar deps
Write-Host ""
Write-Host "[1/3] Instalando dependencias..." -ForegroundColor Yellow
python -m pip install --upgrade pip pyinstaller
python -m pip install -r requirements.txt

# 3. Limpiar builds anteriores
Write-Host ""
Write-Host "[2/3] Limpiando builds anteriores..." -ForegroundColor Yellow
if (Test-Path "packaging\dist")  { Remove-Item -Recurse -Force "packaging\dist"  }
if (Test-Path "packaging\build") { Remove-Item -Recurse -Force "packaging\build" }

# 4. Ejecutar PyInstaller
Write-Host ""
Write-Host "[3/3] Compilando con PyInstaller..." -ForegroundColor Yellow
pyinstaller `
    --noconfirm `
    --distpath "packaging\dist" `
    --workpath "packaging\build" `
    "packaging\mpc-forge.spec"

Write-Host ""
Write-Host "=== Build completo ===" -ForegroundColor Green
Write-Host "Output: packaging\dist\MPC-Forge\"
Write-Host ""
Write-Host "Para probar: packaging\dist\MPC-Forge\MPC-Forge.exe"
Write-Host "Para distribuir: comprime toda la carpeta MPC-Forge en un ZIP."
