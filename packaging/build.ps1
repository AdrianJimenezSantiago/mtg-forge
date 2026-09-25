$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "=== MPC Forge - build local ===" -ForegroundColor Cyan
Write-Host "Working dir: $repoRoot"
Write-Host ""

$py = python --version 2>&1
Write-Host "Python: $py"

Write-Host ""
Write-Host "[1/3] Instalando dependencias..." -ForegroundColor Yellow
python -m pip install --upgrade pip pyinstaller
python -m pip install -r requirements.txt

Write-Host ""
Write-Host "[2/3] Limpiando builds anteriores..." -ForegroundColor Yellow
if (Test-Path "packaging\dist")  { Remove-Item -Recurse -Force "packaging\dist"  }
if (Test-Path "packaging\build") { Remove-Item -Recurse -Force "packaging\build" }

Write-Host ""
Write-Host "[3/3] Compilando con PyInstaller..." -ForegroundColor Yellow
python -m PyInstaller `
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
