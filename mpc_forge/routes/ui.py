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
from mpc_forge.models import Deck, DeckCard
from mpc_forge.paths import static_dir, template_dir
from mpc_forge.services import deck_covers
from mpc_forge.services import i18n as i18n_service
from mpc_forge.services.i18n import LANG_FLAGS, SUPPORTED_LANGS, detect_lang, get_translations

log = logging.getLogger(__name__)

TEMPLATES_DIR = template_dir()
STATIC_DIR = static_dir()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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


templates.env.globals["asset_v"] = _asset_v

templates.env.globals["i18n_v"] = i18n_service.bundle_version

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
    target = request.headers.get("referer") or next_url
    if not is_same_origin(request, target):
        target = "/"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        key="lang",
        value=lang,
        max_age=365 * 24 * 3600,
        httponly=False,
        samesite="lax",
    )
    return response


async def _decks_with_covers(db: AsyncSession, limit: int | None = None) -> list[Deck]:
    """Mazos ordenados por última edición, con recuento y portada del commander.

    En vez de traer TODAS las cartas de TODOS los mazos solo para contarlas
    (lo que hacía ``selectinload(Deck.cards)``), un JOIN con COUNT: con 20
    mazos de 100 cartas pasamos de 2000 filas a 20.

    La portada es el arte que el mazo usa para su commander (ver
    ``services/deck_covers``), resuelta por lotes para todos los mazos.
    """
    stmt = (
        select(Deck, func.count(DeckCard.id).label("card_count"))
        .outerjoin(DeckCard, DeckCard.deck_id == Deck.id)
        .group_by(Deck.id)
        .order_by(Deck.updated_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    deck_rows = (await db.execute(stmt)).all()
    covers = await deck_covers.covers_for_decks(db, [d for d, _ in deck_rows])

    decks = []
    for deck, count in deck_rows:
        cover = covers.get(deck.id, deck_covers.EMPTY)
        deck.card_count = count
        deck.commander_image_url = cover.image_url
        deck.commander_name = cover.name
        decks.append(deck)
    return decks


async def _workshop_status(db: AsyncSession) -> dict:
    """Cifras del panel "Estado del taller" de la landing.

    Cada bloque va por separado y con su propio respaldo: la landing es la
    puerta de entrada de la app y no puede caerse porque, por ejemplo, el
    índice de arte esté a medio migrar.
    """
    from mpc_forge.models import CollectionEntry
    from mpc_forge.services import bulk_data, gdrive_search

    status = {
        "printings": 0, "offline": False,
        "art_files": 0, "art_sources": 0,
        "decks": 0, "collection": 0,
    }
    try:
        local = await bulk_data.local_stats(db)
        status["printings"] = int(local.get("printings") or 0)
        status["offline"] = bool(local.get("syncs"))
    except Exception:
        log.warning("No se pudo leer el estado del volcado de Scryfall", exc_info=True)
    try:
        art = await gdrive_search.stats(db)
        status["art_files"] = int(art.get("total_files") or 0)
        status["art_sources"] = int(art.get("sources_indexed") or 0)
    except Exception:
        log.warning("No se pudo leer el estado del índice de arte", exc_info=True)
    try:
        status["decks"] = int(await db.scalar(select(func.count()).select_from(Deck)) or 0)
        status["collection"] = int(
            await db.scalar(select(func.count()).select_from(CollectionEntry)) or 0
        )
    except Exception:
        log.warning("No se pudieron contar mazos y colección", exc_info=True)
    return status


_SAMPLE_CARD_COLORS = ("w", "u", "b", "r", "g")

_HAND_POSITIONS = (0, -1, 1, -2, 2)


def _build_hand(decks: list[Deck], t) -> list[dict]:
    """Las cinco cartas del abanico de la landing.

    Primero las portadas de los mazos recientes que tienen commander; los
    huecos se rellenan con cartas de ejemplo dibujadas en CSS (sin arte de
    terceros).
    """
    covers = [d for d in decks if d.commander_image_url][: len(_HAND_POSITIONS)]
    slots = []
    for rank, pos in enumerate(_HAND_POSITIONS):
        slot = {"i": pos, "ia": abs(pos), "z": 10 - abs(pos), "rank": rank}
        if rank < len(covers):
            slot["deck"] = covers[rank]
        else:
            n = rank + 1
            slot["color"] = _SAMPLE_CARD_COLORS[rank]
            slot["name"] = t[f"landing_card_{n}_name"]
            slot["type"] = t[f"landing_card_{n}_type"]
        slots.append(slot)
    return sorted(slots, key=lambda s: s["i"])


def _format_number(value: int, lang: str = "es") -> str:
    """Separador de miles según idioma (``12.345`` en español)."""
    text = f"{int(value or 0):,}"
    return text.replace(",", ".") if lang == "es" else text


templates.env.filters["num"] = _format_number


def render_not_found(request: Request, kind: str = "page") -> HTMLResponse:
    """Página 404 con el estilo de la app.

    ``kind`` ajusta el texto: ``"deck"`` cuando la URL es de un mazo que ya
    no existe, ``"page"`` para cualquier otra ruta desconocida. La usan las
    vistas de mazo y el manejador global de ``app.py``.
    """
    return templates.TemplateResponse(
        request,
        "404.html",
        {
            "missing_kind": kind,
            "missing_path": request.url.path,
            **_t_context(request),
        },
        status_code=404,
    )


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: DbDep) -> HTMLResponse:
    """Landing: importación rápida, mazos recientes y resumen del taller."""
    from mpc_forge.clients.import_sites import list_supported_sites

    ctx = _t_context(request)
    recent = await _decks_with_covers(db, limit=8)
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "hand": _build_hand(recent, ctx["t"]),
            "has_covers": any(d.commander_image_url for d in recent),
            "recent_decks": recent[:4],
            "latest_deck": recent[0] if recent else None,
            "workshop": await _workshop_status(db),
            "site_names": [s["name"] for s in list_supported_sites()],
            **ctx,
        },
    )


@router.get("/decks", response_class=HTMLResponse)
async def deck_library(request: Request, db: DbDep) -> HTMLResponse:
    """Biblioteca de mazos (antes vivía en ``/``)."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"decks": await _decks_with_covers(db), **_t_context(request)},
    )


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return render_not_found(request, "deck")
    cover = await deck_covers.cover_for_deck(db, deck)
    commander_image_url = cover.image_url
    return templates.TemplateResponse(
        request,
        "deck.html",
        {"deck": deck, "commander_image_url": commander_image_url, **_t_context(request)},
    )


@router.get("/decks/{deck_id}/proof", response_class=HTMLResponse)
async def proof_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id, options=[selectinload(Deck.cards)])
    if not deck:
        return render_not_found(request, "deck")
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
        return render_not_found(request, "deck")
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
