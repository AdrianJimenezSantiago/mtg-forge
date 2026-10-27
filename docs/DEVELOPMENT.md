# Development

Everything you need to work on MPC Forge itself. For using the app, see the
[README](../README.md).

## Setup

```bash
pip install -r requirements.txt
pip install -e ".[dev]"

pytest -q                                     # test suite
pytest -q --cov=mpc_forge --cov-report=html   # with coverage
ruff check . && ruff format --check .         # lint and format
mypy                                          # type check

uvicorn mpc_forge.app:create_app --factory --reload --port 8765   # dev server
```

## Project layout

```
mpc_forge/
├── app.py            FastAPI factory: middleware, static mounts, routers
├── lifespan.py       startup and shutdown tasks
├── config.py         paths, MPC price tiers, defaults
├── db.py             engine, SQLite pragmas, indexes, FTS5
├── migrations.py     incremental migration ladder
├── models.py         ORM models
├── updater.py        self-update for the executable and for git clones
├── clients/          Scryfall, Moxfield and per-site deck importers
├── routes/           REST endpoints and HTML views (routes.ROUTERS)
│   ├── decks/        deck CRUD, imports, art picker, tokens, activity
│   └── export/       XML, PDF, print shop package, images, decklists, cardbacks, files
├── services/         business logic grouped by domain
│   ├── cards/        Scryfall cache, bulk data, DFC pairs, preloading
│   ├── decks/        importer, parser, validation, covers, undo, snapshots
│   ├── art/          art cache, custom art, thumbnails, library, themes
│   ├── indexing/     art sources, drive indexer and search, source types
│   ├── printing/     PDF, print shop export, XML, calibration, planner, print runs
│   └── system/       settings, storage, backups, logging, i18n, tour
└── utils/            generic helpers (rate limiter, chunking, tasks)
locales/              translations (es.json, en.json)
resources/icc/        colour profiles for print shop exports (with their licence)
templates/
├── base.html         layout
├── pages/            one template per view
└── partials/         sections included by the pages
static/
├── css/              app stylesheets
├── img/              logo and favicon
├── js/core/          shared scripts (API client, shell, toasts, motion, tour)
├── js/pages/         one module per view
├── src/              Tailwind entry point
└── vendor/           third-party assets (generated, committed)
scripts/              asset build, CI checks, release versioning, README media
tests/
├── backend/          API and service tests by domain
├── frontend/         templates, pages, static assets, translations, tour
└── js/               Node scripts that exercise the frontend modules
docs/
├── DEVELOPMENT.md    this file
└── media/            screenshots and the demo GIF used by the README
packaging/            PyInstaller spec, launcher and build scripts
```

## Tests

The suite uses `pytest` with `pytest-asyncio` in `auto` mode, so async tests
need no decorator. Shared fixtures live in `tests/conftest.py`:

- `client` — an `httpx.AsyncClient` bound to the FastAPI app in memory, with a
  fresh temporary database per test.
- `deck` — a pre-imported three-card deck.
- `fake_scryfall` — a mocked Scryfall client; no test touches the network.
  Extend `SAMPLE_CARDS` when a test needs a new card.

Data directories are redirected to a temporary folder before the app is
imported, so running the tests never writes to your real `user-settings/`.

Frontend behaviour that needs real JavaScript (the PDF preview geometry, the
guided tour, `saveFile`) is checked by the scripts in `tests/js/`, run from
pytest through `run_js_test` in `tests/frontend/_support.py`. They need Node and
are skipped without it.

`tests/backend/openapi_snapshot.json` pins the routes the frontend relies on and
their parameters: the suite fails if one disappears or its parameters change.
Update the file when you change an endpoint on purpose.

CI also runs, against a live server: the API client check
(`scripts/check_api_client.mjs`), a jsdom boot of every page
(`scripts/check_pages_in_browser.mjs`), axe-core accessibility
(`scripts/check_accessibility.py`) and Lighthouse on a seeded 300-card deck.

## Frontend assets

The UI uses Tailwind CSS, Alpine.js, Lucide and mana-font, all served locally
from `static/vendor/`. That folder is committed so a clone runs without Node.
You only need Node when changing `tailwind.config.js` or adding new classes:

```bash
npm install
npm run vendor      # copy third-party assets into static/vendor/
npm run build:css   # compile Tailwind
npm run watch:css   # rebuild on change while developing
```

CI checks that `static/vendor/` is up to date on every push.

## Database migrations

To add or change a column:

1. Update the model in `models.py` so new installs get it.
2. Append a `Migration` to `MIGRATIONS` in `migrations.py`, with `version` equal
   to the previous one plus one.
3. Run `pytest tests/backend/test_system.py`.

Never edit or renumber a released migration, and never use `DROP TABLE`,
`DROP COLUMN` or an unqualified `DELETE` — the test suite rejects them.

## Type checking

`mypy` runs in strict mode over `mpc_forge/`. Leave new and touched signatures
annotated.

## README media

The screenshots and the demo GIF in `docs/media/` are generated from a running
app. Start it with a commander deck imported as deck `1` and a second deck for
the planner, then:

```bash
pip install playwright && python -m playwright install chromium
python scripts/capture_readme_media.py http://127.0.0.1:8765 --deck 1
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

To regenerate the icon after editing `packaging/make_icon.py`:

```bash
python packaging/make_icon.py   # writes packaging/icon.ico, static/img/logo.png, static/img/favicon.png
```

## Releases

Every push to `main` is released automatically once its CI run is green:
`.github/workflows/release.yml` works out the next version, runs the tests on
Windows, builds Windows, macOS and Linux binaries with that version stamped in,
and only then creates the tag and the GitHub release (marked as latest, with
generated notes). The version follows semver from the latest `vX.Y.Z` tag and
the commits since it:

| Commits since the last release contain…                                     | Bump                    |
| --------------------------------------------------------------------------- | ----------------------- |
| `[major]`, `BREAKING CHANGE` or `type!:`                                    | major (`2.7.3 → 3.0.0`) |
| `[minor]`, `feat:` or a subject starting with Add, Implement, Introduce…    | minor (`2.7.3 → 2.8.0`) |
| anything else                                                               | patch (`2.7.3 → 2.7.4`) |
| only `[skip release]` commits                                               | no release              |

A patch never goes past `.9` (`2.7.9 → 2.8.0`), so versions stay short. Releases
run one at a time, and a commit that is already part of a release is never
released again, so numbers always go up in commit order. To release by hand or
force a bump, run the *Release* workflow from the Actions tab. Check what the
next version would be with `python scripts/next_version.py plan`.

`mpc_forge/__init__.py` holds the development marker `0.0.0` (shown as *dev* in
the sidebar); never edit it by hand — the release stamps the real version into
the build.
