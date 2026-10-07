from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
from mpc_forge.services.rate_limiter import TieredRateLimiter
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

T = TypeVar("T")

GENERAL_INTERVAL = 0.11
HEAVY_INTERVAL = 0.55

HEAVY_PATHS = frozenset(
    {
        "/cards/search",
        "/cards/named",
        "/cards/random",
        "/cards/collection",
    }
)

RATE_LIMIT_COOLDOWN = 30.0

_RETRY_MAX_ATTEMPTS = 4
_RETRY_MAX_429 = 1
_RETRY_BASE_DELAY = 0.5
_RETRY_MAX_DELAY = 8.0
_RETRYABLE_5XX = {500, 502, 503, 504}

_COLLECTION_CHUNK = 75


def _is_heavy(url: str) -> bool:
    path = httpx.URL(url).path if "://" in url else url.split("?", 1)[0]
    return path.rstrip("/") in HEAVY_PATHS


class ScryfallClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        general_interval: float = GENERAL_INTERVAL,
        heavy_interval: float = HEAVY_INTERVAL,
        cooldown: float = RATE_LIMIT_COOLDOWN,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=SCRYFALL_API,
            headers={
                "User-Agent": SCRYFALL_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=30.0,
            verify=not ssl_insecure(),
        )
        self._limiter = TieredRateLimiter(general_interval, heavy_interval)
        self._cooldown = cooldown
        self._cooldown_until = 0.0
        self._inflight: dict[Any, asyncio.Future[Any]] = {}
        self.stats = {"requests": 0, "rate_limited": 0, "deduplicated": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _start_cooldown(self, seconds: float) -> None:
        until = time.monotonic() + seconds
        if until > self._cooldown_until:
            self._cooldown_until = until

    async def _wait_cooldown(self) -> None:
        while True:
            remaining = self._cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _acquire(self, url: str) -> None:
        heavy = _is_heavy(url)
        while True:
            await self._wait_cooldown()
            await self._limiter.acquire(heavy)
            if not self.cooling_down:
                return

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        rate_limited = 0
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            await self._acquire(url)
            self.stats["requests"] += 1
            try:
                resp = await self._client.request(method, url, params=params, json=json)
            except httpx.RequestError as e:
                last_exc = e
                if attempt == _RETRY_MAX_ATTEMPTS - 1:
                    break
                delay = self._backoff_delay(attempt)
                log.warning(
                    "Scryfall %s %s falló por red (intento %d/%d), reintento en %.1fs: %s",
                    method,
                    url,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429:
                self.stats["rate_limited"] += 1
                wait = max(self._retry_after(resp), self._cooldown)
                self._start_cooldown(wait)
                await resp.aread()
                log.warning(
                    "Scryfall devolvió 429 en %s %s. Pausando TODAS las peticiones %.1fs "
                    "(bloqueo de Scryfall).",
                    method,
                    url,
                    wait,
                )
                if rate_limited < _RETRY_MAX_429:
                    rate_limited += 1
                    continue
                return resp

            if resp.status_code in _RETRYABLE_5XX and attempt < _RETRY_MAX_ATTEMPTS - 1:
                delay = self._backoff_delay(attempt)
                log.warning(
                    "Scryfall %s %s → %d (intento %d/%d), reintento en %.1fs",
                    method,
                    url,
                    resp.status_code,
                    attempt + 1,
                    _RETRY_MAX_ATTEMPTS,
                    delay,
                )
                await resp.aread()
                await asyncio.sleep(delay)
                continue

            return resp

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Scryfall: reintentos agotados sin respuesta")

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        base = min(_RETRY_BASE_DELAY * (2**attempt), _RETRY_MAX_DELAY)
        return base + random.uniform(0, 0.25)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            return max(0.0, float(resp.headers.get("Retry-After", "")))
        except (TypeError, ValueError):
            return 0.0

    async def _singleflight(self, key: Any, factory: Callable[[], Awaitable[T]]) -> T:
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.ensure_future(factory())
            self._inflight[key] = fut

            def _cleanup(f: asyncio.Future[Any]) -> None:
                if self._inflight.get(key) is f:
                    del self._inflight[key]
                if not f.cancelled():
                    f.exception()

            fut.add_done_callback(_cleanup)
        else:
            self.stats["deduplicated"] += 1
        return await asyncio.shield(fut)

    async def _get_uncached(self, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        resp = await self._request_with_retry("GET", path, params=params)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        key = ("GET", path, tuple(sorted((params or {}).items())))
        return await self._singleflight(key, lambda: self._get_uncached(path, params))

    async def _paginate(self, first: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = first
        while page:
            results.extend(page.get("data") or [])
            next_url = page.get("next_page") if page.get("has_more") else None
            if not next_url:
                break
            resp = await self._request_with_retry("GET", next_url)
            resp.raise_for_status()
            page = resp.json()
        return results

    async def search_all(self, query: str, **params: Any) -> list[dict[str, Any]]:
        full = {"q": query, **params}
        key = ("search_all", tuple(sorted(full.items())))

        async def run() -> list[dict[str, Any]]:
            first = await self._get_uncached("/cards/search", full)
            return await self._paginate(first)

        return await self._singleflight(key, run)

    async def named(self, name: str, set_code: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"exact": name}
        if set_code:
            params["set"] = set_code
        return await self._get("/cards/named", params=params)

    async def by_id(self, scryfall_id: str) -> dict[str, Any]:
        return await self._get(f"/cards/{scryfall_id}")

    async def by_set_and_number(
        self, set_code: str, number: str, lang: str | None = None
    ) -> dict[str, Any]:
        path = f"/cards/{set_code.lower()}/{number}"
        if lang and lang != "en":
            path = f"{path}/{lang}"
        return await self._get(path)

    async def prints_by_oracle_id(self, oracle_id: str) -> list[dict[str, Any]]:
        return await self.search_all(
            f"oracleid:{oracle_id} include:extras",
            unique="prints",
            order="released",
            dir="asc",
        )

    async def prints_by_oracle_ids(self, oracle_ids: list[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(o for o in oracle_ids if o))
        if not ids:
            return []
        if len(ids) == 1:
            return await self.prints_by_oracle_id(ids[0])
        clause = " or ".join(f"oracleid:{o}" for o in ids)
        return await self.search_all(
            f"({clause}) include:extras",
            unique="prints",
            order="released",
            dir="asc",
        )

    async def collection(self, identifiers: list[dict[str, str]]) -> list[dict[str, Any]]:
        unique: list[dict[str, str]] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for ident in identifiers:
            k = tuple(sorted(ident.items()))
            if k not in seen:
                seen.add(k)
                unique.append(ident)

        results: list[dict[str, Any]] = []
        for i in range(0, len(unique), _COLLECTION_CHUNK):
            chunk = unique[i : i + _COLLECTION_CHUNK]
            resp = await self._request_with_retry(
                "POST",
                "/cards/collection",
                json={"identifiers": chunk},
            )
            resp.raise_for_status()
            results.extend(resp.json().get("data", []))
        return results

    async def autocomplete(self, query: str) -> list[str]:
        query = query.strip()
        if len(query) < 2 or self.cooling_down:
            return []
        payload = await self._get(
            "/cards/autocomplete",
            params={"q": query, "include_extras": "false"},
        )
        return list(payload.get("data", []) if payload else [])


def is_double_faced(card: dict[str, Any]) -> bool:
    layout = card.get("layout", "normal")
    return layout in {"transform", "modal_dfc", "double_faced_token", "reversible_card"}


def related_parts_from_card(card: dict[str, Any]) -> list[dict[str, str]]:
    self_id = card.get("id")
    out = []
    for part in card.get("all_parts", []) or []:
        pid = part.get("id")
        if not pid or pid == self_id:
            continue
        out.append(
            {
                "id": pid,
                "name": part.get("name", ""),
                "component": part.get("component", ""),
                "type_line": part.get("type_line", ""),
            }
        )
    return out
