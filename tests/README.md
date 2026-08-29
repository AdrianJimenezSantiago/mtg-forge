# Tests

Test suite con pytest + pytest-asyncio.

## Ejecutar

```bash
# Todos los tests
pytest

# Un fichero
pytest tests/test_new_features.py

# Un test específico
pytest tests/test_new_features.py::TestUndo::test_undo_card_moved

# Modo verboso con el nombre de cada test
pytest -v

# Ejecutar hasta el primer fallo (útil al debuggear)
pytest -x
```

## Estructura

- **`conftest.py`** — fixtures compartidas. Redirige paths de datos a `/tmp`,
  reload de `db.py`, cliente httpx en memoria con la app FastAPI y un
  ScryfallClient mockeado (`fake_scryfall`).
- **`test_new_features.py`** — features de la iteración actual: duplicar mazo,
  búsqueda global de cartas, deshacer eventos, progreso de build.
- **`test_imports_and_exports.py`** — imports de Moxfield/texto, reporte de
  cartas no importadas, exportar decklist, localización.
- **`test_moves_and_activity.py`** — mover cartas entre secciones, vaciar
  sección, timeline de actividad.
- **`test_settings.py`** — vista de Ajustes rediseñada, `ssl_insecure` runtime.

## Convenciones

- Todos los tests son `async` — pytest-asyncio en modo `auto` los detecta sin
  necesitar el decorador `@pytest.mark.asyncio` en cada uno.
- Cada test usa el fixture `client` (nuevo por test) y opcionalmente `deck`
  (mazo de prueba precreado con 3 cartas).
- Se agrupan en clases `Test<Feature>` para poder ejecutar toda una feature de
  golpe: `pytest tests/test_new_features.py::TestUndo`.
- El `fake_scryfall` conoce estas cartas por defecto: Sol Ring (varias
  impresiones), Command Tower, Arcane Signet, Lightning Bolt. Ampliar
  `SAMPLE_CARDS` en `conftest.py` cuando un test necesite algo nuevo.
