# MPC Forge

Pipeline local para imprimir mazos de Magic con MakePlayingCards. Importa mazos de Moxfield, elige artes (oficiales de Scryfall + tus artes custom locales) con galería y filtros, cachea las imágenes en tu disco y genera un XML con rutas locales listo para el desktop client de [`chilli-axe/mpc-autofill`](https://github.com/chilli-axe/mpc-autofill).

Pensado para Commander (100 cartas, singleton) — valida el conteo automáticamente y el historial de tiradas es informativo, no restrictivo: cada mazo se lleva su copia, la app te avisa qué has impreso antes pero no te obliga a nada.

- 100% local. Nada en la nube, sin cuenta, sin telemetría.
- Sin dependencia de las Google Drives indexadas de MPCFill: las imágenes las bajas tú desde Scryfall y viven en tu disco.
- **Artes custom locales**: mezcla tus proxies/altered art con las impresiones oficiales, en la misma galería.
- Selector de arte por carta con dedupe estricto (nunca guardarás dos veces el mismo archivo).
- DFC, tokens y emblemas manejados con selector de arte, no automágicamente.
- **Validación de formato**: badge en vivo con el conteo de cartas (100/100 para commander).
- Estimador de coste con sugerencia de relleno al siguiente tier de MPC.
- Backup de todo el estado (BD + artes oficiales + artes custom + cardbacks) a un .zip.

## Requisitos

- **Windows 11** (funciona también en macOS / Linux — el flujo es idéntico).
- **Python 3.11 o superior**. Comprobar con `python --version`. Descargar desde [python.org](https://www.python.org/downloads/windows/) si hace falta. Al instalar marca *"Add Python to PATH"*.

## Instalación

Abre PowerShell en la carpeta donde has descomprimido `mpc-forge/`:

```powershell
cd .\mpc-forge
pip install -r requirements.txt
```

Si quieres que se abra en una ventana propia (en vez del navegador), instala también pywebview:

```powershell
pip install pywebview
```

## Uso

```powershell
python -m mpc_forge
```

Se abre automáticamente el navegador en `http://127.0.0.1:8765`. Para ventana nativa:

```powershell
python -m mpc_forge --window
```

Flags útiles:

- `--no-browser` — no abre el navegador (útil si quieres usar otro).
- `--port 9000` — cambiar puerto.
- `--host 0.0.0.0` — escuchar en toda la red (peligroso, no recomendado).

## Flujo de trabajo

1. **Importa un mazo**. En la home pegas la URL de Moxfield (o el ID solo). Si el mazo es público, se importa. Si es privado o Moxfield está bloqueando, pega la lista en el segundo panel.
2. **Ajusta los artes**. Entra al mazo y pincha en cualquier carta. En el panel derecho ves todas sus impresiones. Filtra por *Full art*, *Sin texto*, *Sin borde* o *Promo*.
   - **Click**: cambia el arte solo en este mazo.
   - **Doble click**: cambia el arte y lo recuerda para futuros mazos que incluyan esta carta.
   - Los badges indican tu elección global (**DEFAULT**), el arte usado en la última tirada (**ÚLTIMO USO**), full art (**FULL**) y textless (**TEXTLESS**).
3. **DFC, tokens y emblemas**. Los mazos que vengan de Moxfield con tokens los verás con badge `TOKENS` en la lista. Selecciona su arte igual que cualquier otra carta.
4. **Cardback**. Coloca tu carta de reverso en `%APPDATA%\MPC-Forge\cardbacks\` con nombre `default-back.png` (o `.jpg`). Se usará automáticamente en el XML.
5. **Estimador de coste**. En el header del mazo tienes el coste MPC estimado en directo y, si te faltan pocas cartas para bajar el precio por carta, un aviso con el ahorro potencial.
6. **Genera XML**. Botón *"Generar XML"* → la app descarga las imágenes que aún no tuviera, dedupica por hash, escribe el XML con rutas absolutas en tu disco y te lo ofrece para descarga. Además queda registrado en el **Historial**.
7. **Envía a MPC**. Abre el desktop client de [`mpc-autofill`](https://github.com/chilli-axe/mpc-autofill/releases) y ábrelo con tu XML. Como usa rutas locales, no toca las Google Drives — todo son tus archivos.

## Dónde vive todo

MPC Forge no toca `Program Files` ni el registro. Todo va a la carpeta de datos del usuario:

```
%APPDATA%\MPC-Forge\
├── mpc_forge.sqlite3   ← base de datos (mazos, elecciones, historial)
├── art\                ← imágenes descargadas de Scryfall (dedupe por SHA256)
│   └── ab\cd\abcdef…png
├── cardbacks\          ← tu(s) cardback(s)
├── exports\            ← XMLs generados
└── backups\            ← zips de backup
```

En macOS es `~/Library/Application Support/MPC-Forge/`. En Linux, `~/.local/share/MPC-Forge/`.

## Backup

En *Ajustes → Crear backup* genera un zip con la BD, los artes y los cardbacks. Se guarda en la carpeta `backups/` de datos. Cópialo donde quieras (Dropbox, disco externo, Git…).

Para restaurar: sustituye el zip por sus contenidos en `%APPDATA%\MPC-Forge\` (o llama a `mpc_forge.services.backup.restore_backup` desde Python).

## Notas y limitaciones

- **Redes corporativas / proxy con SSL propio**. Si tu empresa usa un proxy que intercepta HTTPS (verás `SSL: CERTIFICATE_VERIFY_FAILED` en el terminal), la app usa `truststore` para leer los certificados del almacén de Windows — que ya suele contener la CA de tu empresa. Debería "just work". Si aún así falla, pon la variable de entorno `MPC_FORGE_INSECURE_SSL=1` antes de arrancar para desactivar la verificación (solo úsalo en la red corporativa, no en pública):

  ```powershell
  $env:MPC_FORGE_INSECURE_SSL = "1"; python -m mpc_forge
  ```

- **Moxfield no tiene API pública oficial**. Usamos los endpoints que sirven a su web, con `User-Agent` propio y `cloudscraper` como fallback. Puede romperse en cualquier momento. Si eso ocurre, pega la lista en modo texto — funciona igual.
- **Personaliza el User-Agent**. En `mpc_forge/config.py` cambia `MOXFIELD_USER_AGENT` a algo identificable con tu email/contacto. Es una cortesía y baja la probabilidad de que te bloqueen.
- **Rate limit de Scryfall**: la app respeta ~100 ms entre llamadas. Un mazo de commander con 99 cartas nunca antes vistas tarda ~15-20 segundos la primera vez; la siguiente es instantánea.
- **Historial "commander-aware"**: cada carta te muestra cuántas copias has impreso y en qué mazos, pero **no** te impide re-imprimirla. Es solo informativo, apto para el modelo de un mazo → una copia.
- **Preview visual** de la hoja de impresión: por ahora usa la galería del editor y el preview del desktop client de MPC-Autofill. Roadmap si hace falta.
- **Foil / cardstock** por defecto se pueden editar en `mpc_forge/config.py` (`DEFAULT_CARDSTOCK`, `DEFAULT_CARDBACK_NAME`). La UI aún no expone selector explícito para cambiar por tirada — usa el XML resultante o edítalo.

## Roadmap corto (si lo usas y quieres más)

- Selector de stock / foil en la propia UI antes de generar XML.
- Vista "proof" con cuadrícula previa completa.
- Import desde Archidekt / TappedOut.
- Marcar en el editor cartas que ya has "recibido físicamente" para futuros pedidos (usar `PhysicalInventory` que ya existe en el modelo).
- Splits automáticos cuando un mazo excede 612 cartas.

## Arquitectura (por si tocas código)

```
mpc_forge/
├── app.py              FastAPI factory, lifespan, mount /static y /art
├── __main__.py         entry point (uvicorn + navegador o pywebview)
├── config.py           paths, tiers, User-Agent
├── db.py               SQLAlchemy async + aiosqlite
├── models.py           ORM: PrintingCache, LocalArt, ArtPreference, Deck, DeckCard, PrintRun...
├── schemas.py          Pydantic V2 response models
├── clients/
│   ├── moxfield.py     v3 → v2 → cloudscraper fallback
│   └── scryfall.py     con rate limit y helpers DFC/tokens
├── services/
│   ├── art_cache.py    descarga + dedupe SHA256 con sharding en disco
│   ├── deck_service.py importar mazo, resolver cartas, cargar prints
│   ├── xml_generator.py XML MPC-Autofill con DFC y rutas absolutas
│   ├── cost_estimator.py tiers + sugerencia de relleno
│   ├── history.py      print runs y estadísticas por carta
│   └── backup.py       zip + restore
└── routes/
    ├── ui.py           páginas HTML (Jinja)
    ├── decks.py        API mazos
    └── export.py       API XML, historial, coste, backup

templates/              Jinja + Tailwind (CDN) + Alpine.js — cero build step
static/                 app.css, app.js
```

Frontend: Tailwind y Alpine desde CDN. No hay Node ni bundler. Si quieres modificar el estilo, edita las clases directamente en los `.html`.

## Licencia

MIT (haz lo que quieras). Uso personal; respeta los ToS de Moxfield, Scryfall y MakePlayingCards.

---

Datos que la app **nunca** envía a ningún sitio: tus mazos, tu historial, tus preferencias de arte. Todo lo que sale de tu máquina son las llamadas a Moxfield y Scryfall para leer datos públicos.
