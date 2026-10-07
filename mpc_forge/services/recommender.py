from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from mpc_forge.clients.scryfall import ScryfallClient
from mpc_forge.models import OracleArtistCache

log = logging.getLogger(__name__)

MAX_ORACLES_PER_REQUEST = 25

_FETCH_CONCURRENCY = 8

CACHE_TTL = timedelta(days=7)


def _fold(name: str) -> str:
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(ch for ch in n if not unicodedata.combining(ch))
    n = n.replace("-", " ")
    return " ".join(n.lower().split())


@dataclass
class ArtistMatch:
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None
    image_normal: str | None
    released_at: str | None
    is_full_art: bool
    is_promo: bool


@dataclass
class RecommendResult:
    artist_query: str
    matched: list[ArtistMatch]
    unmatched: list[str]
    skipped: list[str]


def _pick_best_printing(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None

    def _score(c: dict[str, Any]) -> tuple[int, int, str]:
        return (
            1 if c.get("promo") else 0,
            1 if c.get("full_art") else 0,
            "9" if not c.get("released_at") else c["released_at"],
        )

    ranked = sorted(candidates, key=_score)
    best_priority = _score(ranked[0])[:2]
    top_bucket = [
        c
        for c in candidates
        if (1 if c.get("promo") else 0, 1 if c.get("full_art") else 0) == best_priority
    ]
    top_bucket.sort(key=lambda c: c.get("released_at") or "", reverse=True)
    return top_bucket[0]


def _to_match(oracle_id: str, printing: dict[str, Any]) -> ArtistMatch:
    imgs = printing.get("image_uris") or {}
    if not imgs and printing.get("card_faces"):
        imgs = printing["card_faces"][0].get("image_uris", {}) or {}
    return ArtistMatch(
        oracle_id=oracle_id,
        card_name=printing.get("name", ""),
        scryfall_id=printing.get("id", ""),
        set_code=(printing.get("set") or "").lower(),
        set_name=printing.get("set_name") or "",
        collector_number=printing.get("collector_number") or "",
        artist=printing.get("artist") or "",
        image_small=imgs.get("small"),
        image_normal=imgs.get("normal"),
        released_at=printing.get("released_at"),
        is_full_art=bool(printing.get("full_art")),
        is_promo=bool(printing.get("promo")),
    )


async def _lookup_cache(
    db: AsyncSession,
    oracle_ids: list[str],
    artist_folded: str,
) -> tuple[set[str], list[str]]:
    if not oracle_ids:
        return set(), []
    stale_cutoff = datetime.now(UTC) - CACHE_TTL

    rows = (
        await db.execute(
            select(
                OracleArtistCache.oracle_id,
                OracleArtistCache.artist_folded,
                OracleArtistCache.fetched_at,
            ).where(OracleArtistCache.oracle_id.in_(oracle_ids))
        )
    ).all()

    by_oracle: dict[str, list[tuple[str, datetime]]] = {}
    for oid, af, fetched in rows:
        by_oracle.setdefault(oid, []).append((af, fetched))

    with_artist: set[str] = set()
    need_fetch: list[str] = []
    for oid in oracle_ids:
        cached = by_oracle.get(oid)
        if not cached:
            need_fetch.append(oid)
            continue
        any_stale = any(f < stale_cutoff for _, f in cached)
        if any_stale:
            need_fetch.append(oid)
            continue
        if any(af == artist_folded for af, _ in cached):
            with_artist.add(oid)
    return with_artist, need_fetch


async def _persist_cache(
    db: AsyncSession,
    oracle_id: str,
    prints: list[dict[str, Any]],
) -> None:
    artists: set[tuple[str, str]] = set()
    for p in prints:
        candidates: list[str] = []
        if p.get("artist"):
            candidates.append(p["artist"])
        for face in p.get("card_faces") or []:
            if face.get("artist"):
                candidates.append(face["artist"])
        for a in candidates:
            folded = _fold(a)
            if folded:
                artists.add((folded, a))

    await db.execute(delete(OracleArtistCache).where(OracleArtistCache.oracle_id == oracle_id))
    now = datetime.now(UTC)
    for folded, display in artists:
        db.add(
            OracleArtistCache(
                oracle_id=oracle_id,
                artist_folded=folded,
                artist_display=display,
                fetched_at=now,
            )
        )
    if not artists:
        db.add(
            OracleArtistCache(
                oracle_id=oracle_id,
                artist_folded="",
                artist_display="",
                fetched_at=now,
            )
        )


async def recommend_by_artist(
    scryfall: ScryfallClient,
    oracle_ids: list[str],
    artist: str,
    db: AsyncSession | None = None,
) -> RecommendResult:
    artist_folded = _fold(artist)
    if not artist_folded:
        return RecommendResult(artist_query=artist, matched=[], unmatched=[], skipped=[])

    unique_oracles = list(dict.fromkeys(o for o in oracle_ids if o))

    cached_with_artist: set[str] = set()
    to_fetch = unique_oracles
    if db is not None:
        cached_with_artist, to_fetch = await _lookup_cache(
            db,
            unique_oracles,
            artist_folded,
        )

    fetch_now = to_fetch[:MAX_ORACLES_PER_REQUEST]
    skipped_oracles = to_fetch[MAX_ORACLES_PER_REQUEST:]

    fetched_prints = await _fetch_prints_parallel(scryfall, fetch_now)

    if db is not None:
        for oid, prints in fetched_prints.items():
            try:
                await _persist_cache(db, oid, prints)
            except Exception as e:
                log.warning("persist_cache(%s) falló: %s", oid, e)
        await db.commit()

    matched: list[ArtistMatch] = []
    unmatched: list[str] = []

    cached_needing_prints = cached_with_artist - set(fetched_prints.keys())
    extra_fetch = list(cached_needing_prints)[: MAX_ORACLES_PER_REQUEST - len(fetch_now)]
    if extra_fetch:
        fetched_prints.update(await _fetch_prints_parallel(scryfall, extra_fetch))
    skipped_oracles += list(cached_needing_prints - set(extra_fetch))

    for oid in unique_oracles:
        prints = fetched_prints.get(oid, [])
        if not prints:
            if oid in cached_with_artist and oid in skipped_oracles:
                continue
            if oid in cached_with_artist:
                unmatched.append(oid)
                continue
            if oid in fetch_now:
                unmatched.append(oid)
            continue
        candidates: list[dict[str, Any]] = []
        for p in prints:
            artist_candidates = [p.get("artist") or ""]
            for face in p.get("card_faces") or []:
                if face.get("artist"):
                    artist_candidates.append(face["artist"])
            if any(artist_folded in _fold(a) for a in artist_candidates):
                candidates.append(p)
        best = _pick_best_printing(candidates)
        if best:
            matched.append(_to_match(oid, best))
        elif oid in fetch_now:
            unmatched.append(oid)

    return RecommendResult(
        artist_query=artist,
        matched=matched,
        unmatched=unmatched,
        skipped=skipped_oracles,
    )


@dataclass
class StyleMatch:
    oracle_id: str
    card_name: str
    scryfall_id: str
    set_code: str
    set_name: str
    collector_number: str
    artist: str
    image_small: str | None
    image_normal: str | None
    released_at: str | None
    matched_criteria: list[str]


@dataclass
class StyleRecommendResult:
    query: dict[str, Any]
    matched: list[StyleMatch]
    unmatched: list[str]
    skipped: list[str]


def _printing_matches_style(
    printing: dict[str, Any],
    set_code: str | None,
    borderless: bool,
    showcase: bool,
    extended: bool,
    full_art: bool,
) -> list[str]:
    labels: list[str] = []
    if set_code:
        if (printing.get("set") or "").lower() != set_code.lower():
            return []
        labels.append(f"set:{set_code.lower()}")
    if borderless:
        if (printing.get("border_color") or "").lower() != "borderless":
            return []
        labels.append("borderless")
    if showcase:
        effects = printing.get("frame_effects") or []
        if "showcase" not in effects:
            return []
        labels.append("showcase")
    if extended:
        effects = printing.get("frame_effects") or []
        if "extendedart" not in effects:
            return []
        labels.append("extended")
    if full_art:
        if not printing.get("full_art"):
            return []
        labels.append("full_art")
    return labels


async def recommend_by_style(
    scryfall: ScryfallClient,
    oracle_ids: list[str],
    *,
    set_code: str | None = None,
    borderless: bool = False,
    showcase: bool = False,
    extended: bool = False,
    full_art: bool = False,
    db: AsyncSession | None = None,
) -> StyleRecommendResult:
    if not any([set_code, borderless, showcase, extended, full_art]):
        return StyleRecommendResult(
            query={
                "set_code": set_code,
                "borderless": borderless,
                "showcase": showcase,
                "extended": extended,
                "full_art": full_art,
            },
            matched=[],
            unmatched=[],
            skipped=[],
        )

    unique = list(dict.fromkeys(o for o in oracle_ids if o))
    to_fetch = unique[:MAX_ORACLES_PER_REQUEST]
    skipped = unique[MAX_ORACLES_PER_REQUEST:]

    matched: list[StyleMatch] = []
    unmatched: list[str] = []

    prints_by_oid = await _fetch_prints_parallel(scryfall, to_fetch)

    for oid in to_fetch:
        prints = prints_by_oid.get(oid, [])
        if not prints:
            unmatched.append(oid)
            continue

        best: tuple[dict[str, Any], list[str]] | None = None
        for p in prints:
            labels = _printing_matches_style(
                p,
                set_code,
                borderless,
                showcase,
                extended,
                full_art,
            )
            if labels:
                if best is None or (p.get("released_at") or "") > (
                    best[0].get("released_at") or ""
                ):
                    best = (p, labels)

        if best:
            p, labels = best
            imgs = p.get("image_uris") or {}
            if not imgs and p.get("card_faces"):
                imgs = p["card_faces"][0].get("image_uris", {}) or {}
            matched.append(
                StyleMatch(
                    oracle_id=oid,
                    card_name=p.get("name", ""),
                    scryfall_id=p.get("id", ""),
                    set_code=(p.get("set") or "").lower(),
                    set_name=p.get("set_name") or "",
                    collector_number=p.get("collector_number") or "",
                    artist=p.get("artist") or "",
                    image_small=imgs.get("small"),
                    image_normal=imgs.get("normal"),
                    released_at=p.get("released_at"),
                    matched_criteria=labels,
                )
            )
        else:
            unmatched.append(oid)

    return StyleRecommendResult(
        query={
            "set_code": set_code,
            "borderless": borderless,
            "showcase": showcase,
            "extended": extended,
            "full_art": full_art,
        },
        matched=matched,
        unmatched=unmatched,
        skipped=skipped,
    )


async def _fetch_prints_parallel(
    scryfall: ScryfallClient,
    oracle_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not oracle_ids:
        return {}

    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(oid: str) -> tuple[str, list[dict[str, Any]]]:
        async with semaphore:
            try:
                return oid, await scryfall.prints_by_oracle_id(oid)
            except Exception as e:
                log.warning("prints_by_oracle_id(%s) falló: %s", oid, e)
                return oid, []

    results = await asyncio.gather(*(_one(oid) for oid in oracle_ids))
    return dict(results)
