# MTG Forge

> Pipeline local para preparar e imprimir proxies de Magic: The Gathering, con
> soporte de arte custom y flujo directo a MakePlayingCards.

**MTG Forge** (nombre interno: MPC Forge) es una aplicación de escritorio para
Windows que corre en local. Importa mazos desde Moxfield o texto plano, permite
elegir arte oficial o custom para cada carta, y genera:

- Un **XML compatible con [MPC Autofill](https://github.com/chilli-axe/mpc-autofill)**
  para enviar el pedido directamente a MakePlayingCards.
- Un **PDF 3×3 tamaño real** para impresión doméstica en A4 o Letter.

Se distribuye como un `.exe` empaquetado — tus amigos descargan un ZIP, hacen
doble click, y la app se abre en su navegador.

---

## Índice

- [Instalación (usuario)](#instalación-usuario)
- [Uso rápido](#uso-rápido)
- [Configuración recomendada](#configuración-recomendada)
- [Solución de problemas](#solución-de-problemas)
- [Desarrollo](#desarrollo)
- [Publicar una release](#publicar-una-release)
- [Arquitectura](#arquitectura)
- [Licencia](#licencia)

---

## Instalación (usuario)

### Requisitos

- Windows 10 u 11 (64 bits).
- Nada más — el `.exe` incluye Python y todas las dependencias.

### Descargar e instalar

1. Ve a la [pestaña Releases](https://github.com/AdrianJimenezSantiago/mtg-forge/releases)
   y descarga el ZIP más reciente (`MPC-Forge-vX.Y.Z-windows.zip`).
2. Descomprímelo donde quieras (Escritorio, Documentos, etc.).
3. Entra en la carpeta `MPC-Forge` y haz doble click en **`MPC-Forge.exe`**.
4. La primera vez, Windows Defender puede avisar de que es una aplicación no
   verificada. Pulsa **Más información → Ejecutar de todas formas**.
   Es porque no está firmada con un certificado de código (300€/año que no vamos
   a pagar para 4 amigos).
5. Se abrirá una ventana negra de consola con el mensaje de arranque, y a los
   pocos segundos tu navegador por defecto se abrirá en
   [http://127.0.0.1:8765](http://127.0.0.1:8765).

**Para cerrar la app**: cierra la ventana de consola o pulsa `Ctrl+C` en ella.

### Datos y configuración

Todos los datos se guardan en `%APPDATA%\MPC-Forge\`:

```
%APPDATA%\MPC-Forge\
├── mpc_forge.sqlite3       ← BD de mazos, historial, settings, índice
├── art\                    ← Cache de artes descargadas de Scryfall
├── custom_art\             ← Tu arte custom (por URL o carpeta manual)
├── exports\                ← XMLs y PDFs generados
├── backups\                ← Backups manuales
└── logs\mpc-forge.log      ← Log temporal (se borra al cerrar limpio)
```

### Actualizar

Descarga el ZIP nuevo y descomprímelo **encima** de la carpeta anterior.
Sobreescribir todos los archivos es seguro — la BD y el resto de datos están
en `%APPDATA%`, no en la carpeta de instalación.

---

## Uso rápido

### 1. Importar un mazo

Desde la home tienes dos opciones:

- **URL de Moxfield**: pega el enlace (`https://moxfield.com/decks/...`) y pulsa
  importar.
- **Lista de texto**: pega en formato estándar. Soporta tags como `SB:` para
  sideboard y `//Commanders` para separar secciones.

Los reversos de DFC/meld se detectan y añaden automáticamente. Marca la casilla
**"incluir tokens y extras"** si además quieres importar los tokens que las
cartas del mazo generan.

### 2. Elegir arte

En la vista de mazo, click en cualquier carta para abrir el panel de artes.
Aparecen tres secciones:

- **Custom** — lo que hayas añadido manualmente o descargado.
- **Drives comunitarios** — resultados de fuzzy search en los Google Drives
  indexados (ver más abajo).
- **Oficiales (Scryfall)** — todas las impresiones oficiales con filtros por
  tipo de arte.

Un click en un arte lo asigna al mazo. Doble click en un arte oficial lo
"recuerda" globalmente para futuras copias de esa carta.

### 3. Generar XML/PDF

Pulsa **Generar XML** o **Generar PDF** en la barra del mazo. Para el XML, si
tienes MPC Autofill instalado y configurado, aparece un botón **"Enviar a MPC
Autofill"** que lanza el `.exe` directamente con el archivo.

### 4. Imprimir en casa (PDF)

El PDF viene con marcas de corte y tamaño real (63×88 mm). Imprime a escala
100% (nunca "ajustar a página"), corta con guillotina y listo.

---

## Configuración recomendada

Ve a **Ajustes** (`/settings` en el navegador) y configura:

### Google API key (recomendado)

Para que el fuzzy search en los drives comunitarios funcione bien. Es gratis:

1. Ve a [Google Cloud Console — Credentials](https://console.cloud.google.com/apis/credentials).
2. Crea un proyecto nuevo si no tienes ninguno.
3. **Create Credentials → API key**. Cópiala.
4. **APIs & Services → Library**, busca "Google Drive API" y pulsa **Enable**.
5. En MTG Forge, ve a Ajustes y pega la key en **"Google API key (Drive)"**.
6. Vuelve al panel de drives comunitarios y pulsa **"Indexar todos"**. Tardará
   varios minutos, pero solo hace falta una vez (los siguientes indexados se
   saltarán los ya hechos).

Sin API key, la app cae a scraping HTML que solo indexa el primer nivel de cada
drive y con menos precisión.

### MPC Autofill

Descarga el `.exe` desde [github.com/chilli-axe/mpc-autofill/releases](https://github.com/chilli-axe/mpc-autofill/releases)
y en Ajustes de MTG Forge, pega la ruta al `.exe` en **"Ejecutable de MPC
Autofill"**. Así el botón "Enviar a MPC Autofill" funciona.

### Tipo de cambio y envío

En Ajustes puedes personalizar el ratio USD→EUR y el coste de envío
internacional. El estimador de coste usa estos valores.

---

## Solución de problemas

### Windows Defender bloquea el .exe

Es esperado — el ejecutable no está firmado. Pulsa **Más información →
Ejecutar de todas formas**. Ocurre solo la primera vez.

### El navegador no se abre solo

Abre manualmente [http://127.0.0.1:8765](http://127.0.0.1:8765). Si el puerto
8765 está ocupado, cierra otras instancias de la app o cambia con la variable
de entorno `MPC_FORGE_PORT`.

### El indexado de un drive tarda mucho

Los drives grandes tienen decenas de miles de imágenes y muchas carpetas
anidadas. Con la API key activa (recomendado) se paraleliza y va más rápido.
Puedes ver el progreso en vivo en la tarjeta de cada drive (el contador crece
mientras indexa).

### Algo va mal, ¿cómo comparto el log?

Ve a **Ajustes → Log de depuración → ↓ Descargar log**. Se descarga un `.txt`
con todo lo que la app ha hecho en esta sesión. Ábrelo, cópialo o mándalo
para diagnóstico.

Si la app crashea sin poder acceder a la UI, el log también se conserva en
`%APPDATA%\MPC-Forge\logs\mpc-forge.log`.

### SSL / errores de certificado (portátil de empresa)

Si tu red corporativa hace inspección SSL, la app usa el trust store del sistema
por defecto (via `truststore`). Si aun así falla, arranca con:

```powershell
$env:MPC_FORGE_INSECURE_SSL="1"; .\MPC-Forge.exe
```

Esto desactiva la verificación SSL (solo en tu red segura).

---

## Desarrollo

### Setup

Requisitos: Python 3.11+ (recomendado 3.12), `pip`, Git.

```bash
git clone https://github.com/AdrianJimenezSantiago/mtg-forge.git
cd mtg-forge

# (Opcional pero recomendado) entorno virtual
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### Arrancar en modo dev

```bash
python -m mpc_forge
```

Se abrirá en [http://127.0.0.1:8765](http://127.0.0.1:8765).

Flags útiles:

- `--no-browser` — no abre el navegador automáticamente.
- `--port 9000` — cambia el puerto.
- `--host 0.0.0.0` — expone en la red local (por defecto solo `127.0.0.1`).

### Estructura del proyecto

```
mpc_forge/
├── __init__.py, __main__.py       # entry point CLI para dev
├── app.py                         # FastAPI factory + lifespan
├── config.py                      # settings globales y PATHS
├── db.py                          # SQLAlchemy async setup + PRAGMAs SQLite
├── models.py                      # tablas ORM (Deck, DeckCard, IndexedArt...)
├── schemas.py                     # Pydantic schemas de request/response
├── paths.py                       # resolución de rutas dev vs frozen
├── ssl_config.py                  # truststore + fallback insecure
├── clients/
│   ├── moxfield.py                # v3 → v2 → cloudscraper fallback
│   └── scryfall.py                # rate-limited, autocompletado
├── services/
│   ├── deck_service.py            # crear/leer/editar mazos
│   ├── art_cache.py               # descarga y cachea imágenes (SHA256 dedupe)
│   ├── custom_art.py              # arte custom local + desde URL
│   ├── xml_generator.py           # XML para MPC Autofill
│   ├── pdf_generator.py           # PDF 3x3 tamaño real
│   ├── cost_estimator.py          # USD→EUR + envío
│   ├── art_sources.py             # gestor de Google Drives + catálogo
│   ├── gdrive_indexer.py          # indexado vía API v3 o scraping
│   ├── gdrive_search.py           # fuzzy search con scoring por noise_ratio
│   ├── mpc_autofill.py            # detectar y lanzar el .exe
│   ├── settings.py                # KV store + apply_to_config
│   └── logging_setup.py           # FileHandler temporal
└── routes/
    ├── ui.py                      # renderiza plantillas Jinja2
    ├── decks.py                   # CRUD mazos + artes
    ├── export.py                  # generar XML/PDF + estimar coste
    ├── custom_art.py              # gestión arte custom
    ├── settings.py                # GET/PUT settings
    ├── integrations.py            # MPC Autofill + drives + búsqueda
    └── debug.py                   # log info / tail / download

templates/                          # Jinja2 (index, deck, history, settings, proof)
static/                             # CSS custom (Tailwind CDN carga en base.html)
packaging/                          # PyInstaller spec + launcher + build script
.github/workflows/release.yml       # Actions: compila al pushear tag v*
```

### Migrar la BD

Cuando cambies el schema, incrementa `SCHEMA_VERSION` en `mpc_forge/db.py`.
Al detectar que la versión guardada difiere, `init_db()` recrea las tablas.
Se pierden los mazos, historial y settings — pero **no** los artes en disco
(que son la mayoría del valor del usuario).

### Compilar el `.exe` en local

Necesitas estar en Windows con Python 3.11+. Dos formas equivalentes:

**A) Doble click** en `packaging\build.bat` (más simple).

**B) Desde PowerShell**:

```powershell
# Si es la primera vez, desbloquea el script (o usa la opción A que evita esto):
Unblock-File -Path .\packaging\build.ps1

.\packaging\build.ps1
```

Ambas generan `packaging\dist\MPC-Forge\` con el `.exe` y todas sus dependencias.
Comprime esa carpeta en un ZIP para distribuir.

---

## Publicar una release

El workflow de GitHub Actions compila y sube el ZIP automáticamente al pushear
un tag `v*`. Flujo típico:

```bash
# 1. Actualiza CHANGELOG.md con los cambios de la versión

# 2. Commit y push
git add CHANGELOG.md
git commit -m "chore: bump changelog for v0.2.0"
git push

# 3. Crea y pushea el tag
git tag v0.2.0
git push origin v0.2.0
```

En unos minutos, la [pestaña Releases](https://github.com/AdrianJimenezSantiago/mtg-forge/releases)
tendrá el ZIP listo para descargar.

Si quieres probar el workflow sin crear una release oficial, dispáralo
manualmente desde la pestaña **Actions → Release → Run workflow**. Compila y
sube el ZIP como *artifact* del workflow (accesible durante 30 días).

---

## Arquitectura

### Filosofía

- **Local-first**. Cada instalación tiene su propia BD, sus propios artes, y
  su propio índice. No hay servidor central ni cuenta de usuario.
- **Sin backend cloud**. La única dependencia externa es Google Drive API para
  el indexado (opcional) y Scryfall/Moxfield para importar mazos (sin
  autenticación).
- **Ejecutable simple**. Un `.exe`, sin instalador ni servicios en background.

### Stack

- **Backend**: Python 3.11+ / FastAPI / SQLAlchemy async (aiosqlite) / Pydantic V2.
- **HTTP**: httpx async + cloudscraper (fallback anti-Cloudflare para Moxfield)
  + truststore (SSL corporativo).
- **BD**: SQLite en WAL mode con `busy_timeout=30s`. Aguanta indexado
  concurrente sin `database is locked`.
- **Frontend**: Jinja2 SSR + Tailwind CDN + Alpine.js + `mana-font` CDN.
  Sin build step, sin npm — todo es HTML+JS servido tal cual.
- **PDF**: reportlab. **Fuzzy search**: rapidfuzz.
- **Empaquetado**: PyInstaller `--onedir`.

---

## Licencia

[MIT](LICENSE) — haz lo que quieras con el código, pero sin garantías.

**Nota sobre copyright**: MTG Forge no distribuye ni aloja arte de Magic. Las
imágenes oficiales se obtienen dinámicamente de Scryfall (público, ToS permite
uso personal). El arte custom lo proporciona el usuario. Los drives comunitarios
son enlaces a Google Drive de terceros — la app solo consulta sus metadatos vía
la API pública. Uso doméstico y personal.
