"""Fixtures compartidas para toda la test suite.

Estructura:

* ``fake_scryfall`` — un ScryfallClient mockeado con las cartas de prueba
  hardcodeadas. Cero llamadas de red durante los tests.
* ``client`` — un httpx.AsyncClient conectado al app FastAPI en memoria vía
  ASGITransport. Cada test recibe un cliente fresco con una BD temporal.
* Todo el setup de rutas temporales (BD, art, custom, exports, cardbacks) se
  hace UNA vez por sesión de tests para no pagar el coste en cada test.

Convenciones de nombre:
    Fixtures que devuelven un cliente/objeto: nombre en singular ("client").
    Datos de prueba: nombre en plural o con sufijo _data ("sample_cards").
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio


# --- Redirigir directorios de datos ANTES de importar la app ---
# La app lee PATHS al importar; si no reasignamos antes, escribiría en la
# carpeta real del usuario. Hacemos esto a nivel de módulo para que los
# imports posteriores (fixtures, tests) vean los paths correctos.
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mtgforge_pytest_"))
os.environ["APPDATA"] = str(_TMP_ROOT)
os.environ["XDG_DATA_HOME"] = str(_TMP_ROOT)

# Asegurar que el package raíz está en sys.path (permite ejecutar `pytest`
# desde la raíz del proyecto sin instalarlo).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mpc_forge import config as _cfg  # noqa: E402
from mpc_forge.config import Paths  # noqa: E402


def _init_paths() -> Paths:
    """Reescribe cfg.PATHS a las rutas temporales. Crea las carpetas necesarias."""
    data = _TMP_ROOT / "data"
    art = _TMP_ROOT / "art"
    custom = _TMP_ROOT / "custom"
    exp = _TMP_ROOT / "exports"
    bk = _TMP_ROOT / "backups"
    cb = _TMP_ROOT / "cardbacks"
    for d in (data, art, custom, exp, bk, cb):
        d.mkdir(parents=True, exist_ok=True)
    paths = Paths(
        data_dir=data, db_path=data / "db.sqlite3",
        art_dir=art, custom_art_dir=custom, exports_dir=exp,
        backups_dir=bk, cardbacks_dir=cb,
    )
    _cfg.PATHS = paths
    return paths


PATHS = _init_paths()

# Reload de db.py para que capture el path nuevo (el engine se crea al import).
import importlib  # noqa: E402
import mpc_forge.db as _db_mod  # noqa: E402
importlib.reload(_db_mod)


# --- Datos de prueba compartidos ---

def _card_stub(name: str, sfid: str, cn: str, set_code: str = "c21", **overrides):
    """Genera un dict con la forma que devolvería Scryfall para una carta.
    Overrides permite personalizar cualquier campo (tipo, colores, etc.)."""
    base = {
        "id": sfid, "oracle_id": f"oracle-{name.lower().replace(' ', '-')}",
        "name": name, "set": set_code, "set_name": set_code.upper(),
        "collector_number": cn, "rarity": "common", "lang": "en", "layout": "normal",
        "type_line": "Artifact", "mana_cost": "{1}", "cmc": 1.0,
        "colors": [], "color_identity": [],
        "image_uris": {
            "normal": f"https://x.test/{sfid}_n.jpg",
            "large":  f"https://x.test/{sfid}_l.jpg",
            "png":    f"https://x.test/{sfid}.png",
        },
        "artist": "Test Artist", "released_at": "2021-01-01",
        "finishes": ["nonfoil"],
        "keywords": [],
    }
    base.update(overrides)
    return base


# Set canónico de cartas de prueba. Los tests pueden asumir que estas existen.
SAMPLE_CARDS: dict[str, dict] = {
    "Sol Ring": _card_stub("Sol Ring", "sr-en", "263"),
    "Sol Ring ES": _card_stub(
        "Anillo solar", "sr-es", "263",
        oracle_id="oracle-sol-ring",  # mismo oracle, distinta lang
        lang="es",
    ),
    "Sol Ring ALT": _card_stub("Sol Ring", "sr-alt", "999", set_code="mps"),
    "Command Tower": _card_stub(
        "Command Tower", "ct-en", "347",
        type_line="Land", mana_cost="", cmc=0.0,
    ),
    "Arcane Signet": _card_stub("Arcane Signet", "as-en", "234"),
    "Lightning Bolt": _card_stub(
        "Lightning Bolt", "lb-en", "100", set_code="lea",
        type_line="Instant", mana_cost="{R}", cmc=1.0,
        colors=["R"], color_identity=["R"],
        rarity="common", keywords=[],
    ),
}


@pytest.fixture
def sample_cards() -> dict[str, dict]:
    """Diccionario name → Scryfall dict de las cartas de prueba."""
    return SAMPLE_CARDS


@pytest.fixture
def fake_scryfall():
    """Cliente Scryfall mockeado sin llamadas de red.

    Reconoce las cartas de SAMPLE_CARDS por nombre (case-insensitive) o por
    scryfall_id. Todo lo demás devuelve dict/list vacíos.
    """
    _by_id = {v["id"]: v for v in SAMPLE_CARDS.values()}

    async def collection(idents):
        out = []
        seen: set[str] = set()  # evita duplicar Sol Ring cuando llegan varios formatos
        for i in idents:
            name = (i.get("name") or "").lower().strip()
            sfid = i.get("id")
            for k, v in SAMPLE_CARDS.items():
                if v["id"] in seen:
                    continue
                if sfid == v["id"] or (name and v["name"].lower() == name):
                    out.append(v)
                    seen.add(v["id"])
                    break
        return out

    async def by_set_and_number(set_code: str, number: str, lang: str | None = None):
        for v in SAMPLE_CARDS.values():
            if v["set"] == set_code and v["collector_number"] == number:
                # Si piden lang específico, buscamos otra versión del mismo (set, num)
                if lang and v["lang"] != lang:
                    for v2 in SAMPLE_CARDS.values():
                        if (v2["set"] == set_code and v2["collector_number"] == number
                                and v2["lang"] == lang):
                            return v2
                    return {}  # no hay versión localizada
                return v
        return {}

    async def by_id(sfid: str):
        return _by_id.get(sfid, {})

    async def named(name: str, set_code: str | None = None):
        for v in SAMPLE_CARDS.values():
            if v["name"].lower() == name.lower():
                if set_code and v["set"] != set_code:
                    continue
                return v
        return {}

    async def prints_by_oracle_id(oid: str):
        return []

    async def autocomplete(q: str):
        return []

    fake = MagicMock()
    fake.collection = collection
    fake.by_set_and_number = by_set_and_number
    fake.by_id = by_id
    fake.named = named
    fake.prints_by_oracle_id = prints_by_oracle_id
    fake.autocomplete = autocomplete
    fake.aclose = AsyncMock()
    return fake


@pytest_asyncio.fixture
async def client(fake_scryfall) -> AsyncIterator:
    """AsyncClient de httpx conectado a la app FastAPI en memoria.

    Cada test recibe una BD limpia (drop_all + create_all al principio) y su
    propio cliente. La app se recrea entre tests para no arrastrar estado del
    lifespan (aunque para nuestros tests es irrelevante — usamos endpoints
    puros y no dependemos del lifespan).
    """
    from httpx import ASGITransport, AsyncClient
    from mpc_forge.app import create_app
    from mpc_forge.clients.moxfield import MoxfieldClient
    from mpc_forge.db import Base, engine, init_db
    from mpc_forge.services.art_cache import ArtCache

    # Reset limpio de la BD entre tests
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()

    app = create_app()
    app.state.scryfall = fake_scryfall
    app.state.moxfield = MoxfieldClient()
    app.state.art_cache = ArtCache()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def deck(client) -> dict:
    """Crea un mazo de prueba vía import de texto y lo devuelve como dict.

    Contiene 3 cartas en mainboard: Sol Ring, Command Tower, Arcane Signet.
    Útil para tests que necesitan un mazo preexistente sin duplicar setup.
    """
    r = await client.post("/api/decks/import/text", json={
        "name": "Test Deck",
        "text": "1 Sol Ring\n1 Command Tower\n1 Arcane Signet",
        "format": "commander",
    })
    assert r.status_code == 200, r.text
    return r.json()["deck"]
