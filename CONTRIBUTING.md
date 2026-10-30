# Contributing to MPC Forge

Thanks for helping. Bug reports, translations and pull requests are all welcome.

## Before you start

- Read the [development guide](docs/DEVELOPMENT.md): project layout, tests,
  frontend assets, migrations and releases.
- For anything bigger than a small fix, open an issue first so we can agree on
  the approach.

## Making a change

1. Fork and branch from `main`.
2. Set up the environment:

   ```bash
   pip install -r requirements.txt
   pip install -e ".[dev]"
   ```

3. Keep the checks green before you push:

   ```bash
   ruff check . && ruff format --check .
   mypy
   pytest -q --cov=mpc_forge --cov-fail-under=80
   ```

4. If you touched templates or icons, rebuild the committed assets
   (Node 22): `npm ci && npm run vendor && npm run build:css`.

## House rules

- **Every visible string is translated.** Add the key to both
  `locales/es.json` and `locales/en.json` (the tests check they match). API
  errors use `ApiError(status, "code", **params)` with an `error_<code>` key.
- **Migrations never destroy data.** Add a new step at the end of
  `mpc_forge/migrations.py`; no `DROP TABLE` / `DROP COLUMN`.
- **No inline scripts.** Page logic lives in `static/js/`, as the CSP forbids
  inline code.
- **Accessibility.** Modals are labelled dialogs that trap focus, icon-only
  buttons have an accessible name and images have `alt`. CI runs axe-core.
- **Tests guard behaviour.** Add a test for every bug you fix and every
  endpoint you add; keep coverage at 80 % or more.

## Commit messages

Imperative, in English, describing the change (`Add …`, `Fix …`). Commits that
only touch docs or tooling can carry `[skip release]` so `main` does not cut a
new version.
