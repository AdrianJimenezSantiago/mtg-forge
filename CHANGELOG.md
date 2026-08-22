# Changelog

Todos los cambios notables se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) y el proyecto usa
versionado semántico ([SemVer](https://semver.org/lang/es/)).

## [Unreleased]

### Added
- Empaquetado con PyInstaller (modo `--onedir`).
- Workflow de GitHub Actions que compila el `.exe` al pushear un tag `v*`.

## [0.2.0] — 2026-08-22

Primera versión funcional completa.

### Added
- Importación de mazos desde Moxfield (API v3 → v2 → cloudscraper) y desde texto plano.
- Editor de mazos con:
  - Agrupación por rol (commander / mainboard / sideboard / tokens / meld_result).
  - Autocomplete de Scryfall con navegación por teclado.
  - Cambio de arte (frentes y reversos DFC/MDFC/meld) desde galería de impresiones oficiales.
  - Preferencias globales de arte (recordar impresión para una carta en todos los mazos).
  - Detección y añadido automático de reversos meld/DFC.
  - Mana pips oficiales, badges DFC/MDFC/MELD/BATTLE, preview con Ctrl+hover.
- Custom art:
  - Rescan automático de carpeta `custom_art/` local.
  - Añadir por URL (con soporte específico de Google Drive).
  - Reconocimiento de convención `Card Name [BACK] - Variant.ext`.
- Fuzzy search en Google Drives comunitarios:
  - Catálogo curado de 67 drives de MPCFill pre-sembrado.
  - Indexado con Google Drive API v3 (recomendado) o scraping HTML como fallback.
  - Búsqueda instantánea con normalización agresiva y scoring por `noise_ratio`.
- Exportación:
  - XML compatible con MPC Autofill (incluye `<backs>` para cardback global).
  - PDF 3×3 tamaño real (63×88 mm) para impresión doméstica, A4 y Letter.
  - Estimador de coste con conversión USD→EUR + shipping configurable.
- Integración MPC Autofill: detección del `.exe` local y lanzamiento con `--directory`.
- Historial de tiradas de impresión.
- Sistema de settings runtime (edita desde la UI, se aplican sin reiniciar):
  - Tipo de cambio USD/EUR, envío internacional, User-Agent, cardstock por defecto,
    preferencias de arte, ruta al `.exe` de MPC Autofill, Google API key.
- Log temporal a fichero:
  - `%APPDATA%\MPC-Forge\logs\mpc-forge.log`.
  - Se borra al cerrar limpiamente, se conserva si crashea.
  - Descargable desde la UI para diagnóstico.
- Backup manual de BD.
- SQLite en modo WAL con `busy_timeout=30s` — soporta indexado concurrente sin
  errores `database is locked`.
