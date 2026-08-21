@echo off
REM Wrapper para lanzar build.ps1 sin problemas de politica de ejecucion.
REM Doble click en este archivo desde el explorador funciona directamente.

setlocal
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build.ps1"

echo.
pause
