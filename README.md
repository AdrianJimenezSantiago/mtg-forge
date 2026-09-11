<div align="center">

<img src="static/logo.png" alt="MPC Forge" width="120">

# MPC Forge

**A local-first workflow for printing Magic: The Gathering proxies.**

Paste a decklist, pick the art for every card, and export a print-ready PDF or
an MPC Autofill XML — without leaving your computer.

[![CI](https://github.com/AdrianJimenezSantiago/mtg-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/AdrianJimenezSantiago/mtg-forge/actions/workflows/ci.yml)
[![Release](https://github.com/AdrianJimenezSantiago/mtg-forge/actions/workflows/release.yml/badge.svg)](https://github.com/AdrianJimenezSantiago/mtg-forge/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)

</div>

---

## Overview

```
Moxfield · Archidekt · Deckstats · TappedOut · MTGGoldfish · CubeCobra · Scryfall · plain text
                                        │
                              card resolution (Scryfall)
                                        │
          art picker  ◄──  official printings · community Google Drives · your own images
                                        │
            MPC Autofill XML  ·  printable PDF  ·  image ZIP  ·  decklist.txt
```

Everything lives on your machine: no account, no server, no telemetry. The
interface is served by the app itself and works offline once your card data and
art are cached.

## Features

- **Import from anywhere.** Paste a URL from any of the supported deck sites, or
  a plain-text list in the usual formats (`1 Sol Ring`, `1 Sol Ring (C21) 263`,
  section headers for commander, sideboard and maybeboard). Cards that cannot be
  resolved are reported back with the original line so you can fix them.
- **Art picker over community drives.** Indexes the public MPC Autofill Google
  Drives with SQLite FTS5 and makes them searchable instantly, with filters for
  full art, borderless, extended, showcase, retro frame, artist and set.
  Perceptual hashing removes duplicates across drives.
- **Art library.** Browse the whole index without starting from a card: an
  artist's work, every full-art print of a set, and so on.
- **PDF Studio.** Fine control over home printing: margins, bleed, per-card and
  per-page cut guides, square or rounded corners, duplex with drift
  compensation, and card backs for DFCs or the whole deck.
- **Duplex calibration.** A two-page test sheet turns "the back comes out
  misaligned" into two offsets you can apply in PDF Studio.
- **Print planner.** Combine several decks into MakePlayingCards orders and see
  which bracket each order lands in before you buy.
- **Collection.** Track the cards you actually own, by set, and cross-reference
  them with your decks to print only what is missing.
- **History and undo.** Every change to a deck is recorded on a timeline and can
  be reverted individually.
- **Offline mode (API only for now).** Optionally import Scryfall's bulk data so
  card resolution and printing lookups never touch the network. See
  [Working with Scryfall](#working-with-scryfall).
- **Bilingual interface.** English and Spanish.

## Installation

### Windows executable (recommended)

1. Download the `.zip` from the [latest release](https://github.com/AdrianJimenezSantiago/mtg-forge/releases/latest).
2. Extract it anywhere.
3. Run `MPC-Forge.exe`.

No installer or administrator rights are needed. The app opens in your default
browser at `http://127.0.0.1:8765`. Set the `MPC_FORGE_PORT` environment
variable to use a different port.

### From source

Requires Python 3.11 or later.

```bash
git clone https://github.com/AdrianJimenezSantiago/mtg-forge.git
cd mtg-forge
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m mpc_forge
```

| Option         | Description                                                    |
| -------------- | -------------------------------------------------------------- |
| `--port N`     | Listen on port `N` (default `8765`).                           |
| `--host H`     | Bind address (default `127.0.0.1`).                            |
| `--no-browser` | Do not open a browser tab on startup.                          |
| `--window`     | Open in a native window instead (`pip install pywebview`).     |

Node.js is **not** required: the compiled frontend assets are committed in
`static/vendor/`.

## Getting started

1. **Import a deck** from the home screen, by URL or by pasting a list.
2. **Add art sources** (optional, recommended). *Settings → Art sources* ships
   with the public MPC Autofill drives preconfigured; press *Index* to make them
   searchable. A [Google Drive API key](https://console.cloud.google.com/apis/credentials)
   makes indexing much faster and more reliable; without one the app falls back
   to scraping.
3. **Choose the art.** In the deck editor, click any card to open the picker
   with every official printing plus everything your drives have for it.
4. **Export.**
   - *Generate XML* — open it with the
     [MPC Autofill desktop client](https://github.com/chilli-axe/mpc-autofill/releases)
     to order from MakePlayingCards.
   - *PDF Studio* — a PDF for your own printer or a print shop, or *Export ZIP*
     for one image per card.

### Custom art

Drop images into the `custom_art/` folder and they appear in the picker next to
the official printings. The file name decides the match:

| File name                     | Matches                                   |
| ----------------------------- | ----------------------------------------- |
| `Sol Ring.png`                | Sol Ring, front face                      |
| `Sol Ring - Anime.png`        | Sol Ring, "Anime" variant                 |
| `Sol Ring (Retro Frame).png`  | Sol Ring, "Retro Frame" variant           |
| `Delver of Secrets [BACK].png`| Delver of Secrets, back face              |
| `Opt [DMU 100].png`           | That exact Scryfall printing              |

## Your data

The app is **portable by default**: if it can write next to the executable (or
the project root when running from source) it uses a `user-settings/` folder
there. Otherwise — for example when installed under `Program Files` — it falls
back to the operating system's per-user data directory.

```
user-settings/
├── mpc_forge.sqlite3   decks, history, collection, drive index
├── art/                images downloaded from Scryfall (deduplicated by SHA-256)
├── thumbs/             generated WebP thumbnails (safe to delete)
├── custom_art/         your own images
├── cardbacks/          custom card backs
├── exports/            generated XML, PDF and ZIP files
├── backups/            backups, including automatic pre-migration ones
└── logs/               log of the current session
```

Every location except the database can be changed in *Settings → Paths*.

> **Updating never deletes your data.** Schema migrations are incremental, and
> an automatic backup is written to `backups/` before any of them runs.

## Working with Scryfall

Card data comes from the [Scryfall API](https://scryfall.com/docs/api). The
client follows Scryfall's [published rate limits](https://scryfall.com/docs/api/rate-limits)
so that importing and opening many decks in a row never gets the app blocked:

- **Per-endpoint limits.** Search, named lookup and collection requests are
  spaced at 2 per second; every other endpoint at 10 per second, with a safety
  margin on both.
- **Global back-off.** If Scryfall ever answers `429 Too Many Requests`, *all*
  requests pause for the 30-second block instead of retrying through it.
- **Fewer requests in the first place.** Cards already in the local cache are
  resolved without the network, identical in-flight requests are shared, and
  alternate printings for a whole deck are fetched with a handful of combined
  searches rather than one search per card.
- **Offline mode.** Importing Scryfall's bulk data (about 1 GB on disk) turns
  lookups into local queries. There is no button for it in the interface yet;
  start it from the interactive API docs at `http://127.0.0.1:8765/docs` with
  `POST /api/bulk/sync`, and follow progress with `GET /api/bulk/progress`.

Images are downloaded from `cards.scryfall.io` and cached on disk.

## Development

```bash
pip install -r requirements.txt
pip install -e ".[dev]"

pytest -q                                     # test suite
pytest -q --cov=mpc_forge --cov-report=html   # with coverage
ruff check mpc_forge tests                    # lint
mypy mpc_forge --ignore-missing-imports       # type check

uvicorn mpc_forge.app:create_app --factory --reload --port 8765   # dev server
```

### Tests

The suite uses `pytest` with `pytest-asyncio` in `auto` mode, so async tests
need no decorator. Shared fixtures live in `tests/conftest.py`:

- `client` — an `httpx.AsyncClient` bound to the FastAPI app in memory, with a
  fresh temporary database per test.
- `deck` — a pre-imported three-card deck.
- `fake_scryfall` — a mocked Scryfall client; no test touches the network.
  Extend `SAMPLE_CARDS` when a test needs a new card.

Data directories are redirected to a temporary folder before the app is
imported, so running the tests never writes to your real `user-settings/`.

### Frontend assets

The UI uses Tailwind CSS, Alpine.js, Lucide, Chart.js and mana-font, all served
locally from `static/vendor/`. That folder is committed so a clone runs without
Node. You only need Node when changing `tailwind.config.js` or adding new
classes:

```bash
npm install
npm run vendor      # copy third-party assets into static/vendor/
npm run build:css   # compile Tailwind
npm run watch:css   # rebuild on change while developing
```

CI checks that `static/vendor/` is up to date on every push.

### Database migrations

To add or change a column:

1. Update the model in `models.py` so new installs get it.
2. Append a `Migration` to `MIGRATIONS` in `migrations.py`, with `version` equal
   to the previous one plus one.
3. Run `pytest tests/test_migrations.py`.

Never edit or renumber a released migration, and never use `DROP TABLE`,
`DROP COLUMN` or an unqualified `DELETE` — the test suite rejects them.

### Type checking

`mypy` runs in permissive mode and is being tightened module by module. When
you touch a module in `services/`, leave its signatures annotated.

### Project layout

```
mpc_forge/
├── app.py            FastAPI factory and lifecycle
├── config.py         paths, MPC price tiers, defaults
├── db.py             engine, SQLite pragmas, indexes, FTS5
├── migrations.py     incremental migration ladder
├── models.py         ORM models
├── clients/          Scryfall, Moxfield and per-site deck importers
├── routes/           REST endpoints and HTML views
└── services/         business logic, one responsibility per module
templates/            Jinja2 templates
static/
├── js/               frontend ES modules
├── src/              Tailwind entry point
└── vendor/           third-party assets (generated, committed)
scripts/              asset build and CI helper scripts
tests/                pytest suite
packaging/            PyInstaller spec, launcher and build scripts
```

## Building the Windows executable

The executable is built with PyInstaller in `--onedir` mode: it starts
instantly, triggers fewer antivirus false positives, and updating is just
replacing the folder.

```powershell
pip install -e ".[build]"
.\packaging\build.ps1          # or double-click packaging\build.bat
```

The output is `packaging\dist\MPC-Forge\`; zip that folder to distribute it.
Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds on
`windows-latest` and attaches the zip to the GitHub release.

To regenerate the icon after editing `packaging/make_icon.py`:

```bash
python packaging/make_icon.py   # writes packaging/icon.ico, static/logo.png, static/favicon.png
```

## Credits and disclaimer

- Card data and official images: [Scryfall](https://scryfall.com).
- XML format and community drives: [MPC Autofill](https://github.com/chilli-axe/mpc-autofill).
- Mana symbols: [mana-font](https://github.com/andrewgioia/mana).

This tool is intended for personal use: making playtest copies of cards you do
not own, to play at home. It is not affiliated with Wizards of the Coast,
Scryfall or MakePlayingCards. Magic: The Gathering is a trademark of Wizards of
the Coast. Do not sell proxies.

Released under the [MIT License](LICENSE).
