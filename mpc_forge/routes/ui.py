from __future__ import annotations

import logging

from fastapi import APIRouter, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.import_sites import list_supported_sites
from mpc_forge.middleware import is_same_origin
from mpc_forge.models import CollectionEntry, Deck
from mpc_forge.paths import static_dir, template_dir
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.services.cards import bulk_data
from mpc_forge.services.decks import deck_covers
from mpc_forge.services.indexing import gdrive_search
from mpc_forge.services.system import i18n as i18n_service
from mpc_forge.services.system.i18n import (
    BASE_LANG,
    LANG_FLAGS,
    SUPPORTED_LANGS,
    detect_lang,
    get_translations,
)

log = logging.getLogger(__name__)

TEMPLATES_DIR = template_dir()
STATIC_DIR = static_dir()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_ASSET_MTIMES: dict[str, int] = {}


def _asset_v(filename: str) -> int:
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


def _t_context(request: Request) -> dict:
    lang = detect_lang(request)
    return {"t": get_translations(lang), "lang": lang}


_I18N_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get("/i18n/{lang}.js")
async def i18n_bundle(lang: str) -> Response:
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
    if lang not in dict(SUPPORTED_LANGS):
        lang = BASE_LANG
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
    decks = []
    for deck, count, cover in await deck_covers.decks_with_covers(db, limit):
        deck.card_count = count
        deck.commander_image_url = cover.image_url
        deck.commander_name = cover.name
        decks.append(deck)
    return decks


async def _workshop_status(db: AsyncSession) -> dict:
    summary = {
        "printings": 0,
        "offline": False,
        "art_files": 0,
        "art_sources": 0,
        "decks": 0,
        "collection": 0,
    }
    try:
        local = await bulk_data.local_stats(db)
        summary["printings"] = int(local.get("printings") or 0)
        summary["offline"] = bool(local.get("syncs"))
    except Exception:
        log.warning("No se pudo leer el estado del volcado de Scryfall", exc_info=True)
    try:
        art = await gdrive_search.stats(db)
        summary["art_files"] = int(art.get("total_files") or 0)
        summary["art_sources"] = int(art.get("sources_indexed") or 0)
    except Exception:
        log.warning("No se pudo leer el estado del índice de arte", exc_info=True)
    try:
        summary["decks"] = int(await db.scalar(select(func.count()).select_from(Deck)) or 0)
        summary["collection"] = int(
            await db.scalar(select(func.count()).select_from(CollectionEntry)) or 0
        )
    except Exception:
        log.warning("No se pudieron contar mazos y colección", exc_info=True)
    return summary


_SAMPLE_CARD_COLORS = ("w", "u", "b", "r", "g")

_HAND_POSITIONS = (0, -1, 1, -2, 2)


def _build_hand(decks: list[Deck], t) -> list[dict]:
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
    text = f"{int(value or 0):,}"
    return text.replace(",", ".") if lang == "es" else text


templates.env.filters["num"] = _format_number


def render_not_found(request: Request, kind: str = "page") -> HTMLResponse:
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
    return templates.TemplateResponse(
        request,
        "index.html",
        {"decks": await _decks_with_covers(db), **_t_context(request)},
    )


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
async def deck_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id)
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
    deck = await db.get(Deck, deck_id)
    if not deck:
        return render_not_found(request, "deck")
    return templates.TemplateResponse(
        request,
        "proof.html",
        {"deck": deck, **_t_context(request)},
    )


@router.get("/decks/{deck_id}/pdf", response_class=HTMLResponse)
async def pdf_studio_page(deck_id: int, request: Request, db: DbDep) -> HTMLResponse:
    deck = await db.get(Deck, deck_id)
    if not deck:
        return render_not_found(request, "deck")
    return templates.TemplateResponse(
        request,
        "pdf_studio.html",
        {"deck": deck, **_t_context(request)},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "history.html", _t_context(request))


@router.get("/print-planner", response_class=HTMLResponse)
async def print_planner_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "print_planner.html", _t_context(request))


@router.get("/art-library", response_class=HTMLResponse)
async def art_library_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "art_library.html", _t_context(request))


@router.get("/calibrate", response_class=HTMLResponse)
async def calibration_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "calibration.html", _t_context(request))


@router.get("/collection", response_class=HTMLResponse)
async def collection_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "collection.html", _t_context(request))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "settings.html", _t_context(request))
