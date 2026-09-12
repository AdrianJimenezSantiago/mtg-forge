"""Endpoints de mazos, repartidos por área funcional.

Este paquete sustituye al antiguo `routes/decks.py` de 2.226 líneas. El corte
es puramente organizativo: las rutas HTTP, sus firmas y sus respuestas son
idénticas a las de antes.

* ``search`` — Búsqueda de cartas a través de todos los mazos del usuario.
* ``activity`` — Línea de tiempo por mazo y deshacer de eventos concretos.
* ``localize`` — Cambio de idioma de las impresiones y autocompletado de nombres.
* ``imports`` — Importación de mazos desde Moxfield, URLs de terceros y texto plano.
* ``tokens`` — Análisis de tokens del mazo y adición masiva de partes relacionadas.
* ``art`` — Selector de artes, precarga en segundo plano y recomendadores.
* ``crud`` — Alta, consulta, edición y borrado de mazos y de sus cartas.

Todos los sub-routers comparten el prefijo `/api/decks`, así que el orden en
que se incluyen importa: FastAPI resuelve por orden de registro, y las rutas
literales como `/_/search-cards` tienen que ir ANTES que las paramétricas como
`/{deck_id}` o serían capturadas por ellas.
"""
from __future__ import annotations

from fastapi import APIRouter

from mpc_forge.routes.decks import activity, art, crud, imports, localize, search, tokens

router = APIRouter()

# ORDEN DELIBERADO. `search`, `activity` y `localize` exponen rutas bajo el
# prefijo literal `/_/`, que debe registrarse antes que `crud`, dueño de
# `/{deck_id}`. Con el orden invertido, una petición a `/api/decks/_/autocomplete`
# entraría por `/{deck_id}` con deck_id="_" y devolvería un 422.
router.include_router(search.router)
router.include_router(activity.router)
router.include_router(localize.router)
router.include_router(imports.router)
router.include_router(tokens.router)
router.include_router(art.router)
router.include_router(crud.router)

__all__ = ["router"]
