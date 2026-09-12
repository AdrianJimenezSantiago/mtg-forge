"""Rutas de la UI (HTML). Todo el frontend usa Tailwind (CDN) + Alpine.js + fetch()."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from mpc_forge.db import get_session
from mpc_forge.models import Deck, DeckCard, PrintingCache
from mpc_forge.paths import static_dir, template_dir
from mpc_forge.services import i18n as i18n_service
from mpc_forge.services.i18n import LANG_FLAGS, SUPPORTED_LANGS, detect_lang, get_translations

log = logging.getLogger(__name__)

TEMPLATES_DIR = template_dir()
STATIC_DIR = static_dir()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Cache-busting: los templates usan `{{ asset_v('app.js') }}` para generar URLs
# como `/static/app.js?v=1735234123`. El navegador guarda /static/app.js en cache
# con max-age=1día (nuestra CachedStaticFiles), pero cuando cambiamos el fichero
# y reiniciamos, el `v` cambia → URL nueva → cache-miss → descarga fresca.
#
# El mtime se lee UNA VEZ al arrancar la app. No hay hit por cada request. Si
# el usuario reinicia el .exe, el asset se recarga.
_ASSET_MTIMES: dict[str, int] = {}


def _asset_v(filename: str) -> int:
    """Devuelve un entero único por versión de asset (mtime del fichero).

    Cacheado en memoria: la primera llamada lee mtime del disco, las
    siguientes devuelven el valor guardado. Si el asset no existe devuelve 0
    (URL sin query — el navegador cacheará normalmente, lo cual es OK).
    """
    if filename not in _ASSET_MTIMES:
        path = STATIC_DIR / filename
        try:
            _ASSET_MTIMES[filename] = int(path.stat().st_mtime)
        except OSError:
            _ASSET_MTIMES[filename] = 0
    return _ASSET_MTIMES[filename]


# Exponemos la función a los templates como global (accesible desde cualquier
# {% extends %} o {% include %}).
templates.env.globals["asset_v"] = _asset_v

# Versión del bundle de traducciones, para el cache-busting de /i18n/<lang>.js.
templates.env.globals["i18n_v"] = i18n_service.bundle_version

# Exponer constantes i18n a todos los templates
templates.env.globals["SUPPORTED_LANGS"] = SUPPORTED_LANGS
templates.env.globals["LANG_FLAGS"] = LANG_FLAGS

router = APIRouter(tags=["ui"])

DbDep = Annotated[AsyncSession, Depends(get_session)]


def _t_context(request: Request) -> dict:
    """Devuelve el contexto i18n que se añade a todos los templates.

    - ``t``    → objeto Translations con acceso tipo ``t.nav_decks``
    - ``lang`` → código de idioma activo ("es" | "en")
    - ``_T``   → dict completo para inyectar en ``window._T`` desde JS
    """
    lang = detect_lang(request)
    tr = get_translations(lang)
    return {"t": tr, "lang": lang, "_T": tr.as_dict()}


# Cache de un año: la URL lleva el hash del contenido (`?v=`), así que un
# cambio en las traducciones genera una URL distinta. `immutable` evita incluso
# la petición condicional al refrescar.
_I18N_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get("/i18n/{lang}.js")
async def i18n_bundle(lang: str) -> Response:
    """Sirve las traducciones como JavaScript cacheable.

    Antes esto era un bloque ``<script>`` inline en ``base.html``: las ~740
    cadenas (unos 25 KB) viajaban dentro del HTML en CADA navegación, y al ir
    embebidas en el documento el navegador no podía cachearlas por separado.

    Como fichero aparte con URL versionada se descarga una sola vez. Sigue
    definiendo exactamente los mismos globales (``window._T``, ``window._LANG``,
    ``window._t``) y en el mismo punto del documento, así que el orden de
    ejecución respecto al resto de scripts no cambia.
    """
    if lang not in dict(SUPPORTED_LANGS):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Idioma no soportado: {lang}")
    return Response(
        content=i18n_service.bundle_js(lang),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": _I18N_CACHE_CONTROL},
    )


@router.post("/set-lang", response_class=HTMLResponse)
async def set_language(
    request: Request,
    lang: str = Form(...),
    next_url: str = Form(default="/"),
) -> Response:
    """Cambia el idioma de la UI guardándolo en una cookie de larga duración."""
    from mpc_forge.middleware import is_same_origin
    from mpc_forge.services.i18n import _TRANSLATIONS
    if lang not in _TRANSLATIONS:
        lang = "es"
    # Redirect al referer o a la home — mantiene al usuario en la página actual.
    #
    # El destino se valida: `Referer` lo controla el cliente, así que sin
    # comprobarlo esto sería un redirect abierto (alguien enlaza a
    # /set-lang con un Referer a su web y el usuario acaba allí creyendo que
    # sigue en la app).
    target = request.headers.get("referer") or next_url
    if not is_same_origin(request, target):
        target = "/"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        key="lang",
        value=lang,
        max_age=365 * 24 * 3600,  # 1 año
        httponly=False,            # JS puede leer window._LANG si hace falta
        samesite="lax",
    )
    return response


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: DbDep) -> HTMLResponse:
    # Optimización: en vez de traer TODAS las cartas de TODOS los mazos solo
    # para contar el len (lo que hacía selectinload(Deck.cards)), hacemos un
    # JOIN con COUNT. Con 20 mazos × 100 cartas pasamos de traer 2000 rows
    # a solo 20 filas con el count agregado.
    result = await db.execute(
        select(Deck, func.count(DeckCard.id).label("card_count"))
        .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
        .group_by(Deck.id)
        .order_by(Deck.updated_at.desc())
    )
    deck_rows = result.all()

    # Batch: printings de los commanders para pintar la card con su arte.
    # Mismo patrón que list_decks_with_activity — WHERE ... IN (?) en una sola
    # query en lugar de N gets individuales.
    commander_ids = {d.commander_scryfall_id for d, _ in deck_rows if d.commander_scryfall_id}
    printings_by_id: dict[str, PrintingCache] = {}
    if commander_ids:
        rows = (
            await db.scalars(
                select(PrintingCache).where(PrintingCache.scryfall_id.in_(commander_ids))
            )
        ).all()
        printings_by_id = {p.scryfall_id: p for p in rows}

    decks = []
    for deck, count in deck_rows:
        deck.card_count = count  # atributo runtime, disponible en el template
        # Arte del commander (o None si no hay commander / printing sin cache).
        printing = printings_by_id.get(deck.commander_scryfall_id) if deck.commander_scryfall_id else None
        deck.commander_image_url = printing.image_normal if printing else None
        deck.commander_name = printing.name if printing else None
        decks.append(deck)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"decks": decks, **_t_context(request)},
    )


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return HTMLResponse("Deck no encontrado", status_code=404)
    return templates.TemplateResponse(
        request,
        "deck.html",
        {"deck": deck, **_t_context(request)},
    )


@router.get("/decks/{deck_id}/proof", response_class=HTMLResponse)
async def proof_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return HTMLResponse("Deck no encontrado", status_code=404)
    return templates.TemplateResponse(
        request,
        "proof.html",
        {"deck": deck, **_t_context(request)},
    )


@router.get("/decks/{deck_id}/pdf", response_class=HTMLResponse)
async def pdf_studio_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    """PDF Studio — layout 3-columnas (config | preview | ajustes) al estilo
    proxxied.com. La página carga los datos del mazo vía fetch, no por context,
    para que el mismo template no tenga que preocuparse por serializar cartas.
    """
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return HTMLResponse("Deck no encontrado", status_code=404)
    return templates.TemplateResponse(
        request,
        "pdf_studio.html",
        {"deck": deck, **_t_context(request)},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    """Vista de historial. Los datos (mazos, runs, timelines) se cargan vía
    fetch desde el frontend — el template no necesita context inicial."""
    return templates.TemplateResponse(request, "history.html", _t_context(request))


@router.get("/print-planner", response_class=HTMLResponse)
async def print_planner_page(request: Request) -> HTMLResponse:
    """Planificador de tiradas: reparte varios mazos en pedidos de MPC.

    Sin contexto inicial a propósito. La lista de mazos y el plan se piden por
    fetch, porque el plan se recalcula cada vez que el usuario marca o desmarca
    un mazo y no tendría sentido renderizar uno en servidor que quedaría
    obsoleto al primer clic.
    """
    return templates.TemplateResponse(
        request, "print_planner.html", _t_context(request)
    )


@router.get("/art-library", response_class=HTMLResponse)
async def art_library_page(request: Request) -> HTMLResponse:
    """Biblioteca de arte: explorador del índice de drives.

    Sin contexto inicial: los filtros viven en la query string y la vista los
    lee en el cliente, de forma que una búsqueda concreta se pueda guardar en
    marcadores o compartir.
    """
    return templates.TemplateResponse(
        request, "art_library.html", _t_context(request)
    )


@router.get("/calibrate", response_class=HTMLResponse)
async def calibration_page(request: Request) -> HTMLResponse:
    """Asistente de calibración de dúplex."""
    return templates.TemplateResponse(
        request, "calibration.html", _t_context(request)
    )


@router.get("/collection", response_class=HTMLResponse)
async def collection_page(request: Request) -> HTMLResponse:
    """Vista de colección — tracking de cartas por set."""
    return templates.TemplateResponse(request, "collection.html", _t_context(request))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", _t_context(request))
