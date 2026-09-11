"""Cliente async para la API de Scryfall.

Límites oficiales (https://scryfall.com/docs/api/rate-limits):

* ``/cards/search``, ``/cards/named``, ``/cards/random``, ``/cards/collection``
  → **2 peticiones/s** (500 ms).
* Resto de endpoints → **10 peticiones/s** (100 ms).
* Un HTTP 429 bloquea el acceso durante **30 s**. Reintentar antes de que pase
  ese tiempo cuenta como abuso y puede acabar en un baneo de la IP.
* Los ficheros de ``*.scryfall.io`` (imágenes, bulk data) no tienen límite.

Cómo se respetan
----------------
1. **Dos límites simultáneos.** Cada petición reserva su instante de salida
   a 110 ms de cualquier otra y, si es de un endpoint pesado, a 550 ms de la
   pesada anterior (ver :class:`TieredRateLimiter`). El ~10 % de margen
   absorbe el jitter de red: dos peticiones que salen separadas 100 ms pueden
   llegar a 90 ms.
2. **Enfriamiento global tras un 429.** Si Scryfall responde 429, *todas* las
   peticiones del cliente se pausan hasta que pase el bloqueo (30 s o el
   ``Retry-After`` si es mayor). Antes solo se reintentaba la petición que
   había fallado, con un tope de 8 s, mientras el resto seguía disparando:
   exactamente lo que Scryfall pide no hacer.
3. **Deduplicación en vuelo.** Dos llamadas idénticas simultáneas (p. ej. dos
   mazos abiertos a la vez que comparten Sol Ring) comparten una sola
   petición HTTP.

El cliente es único por proceso (``app.state.scryfall``), así que los límites
se aplican a toda la aplicación aunque haya varios imports o precargas a la vez.
"""
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

# Espaciado mínimo entre inicios de petición, con ~10 % de margen sobre el
# límite oficial.
GENERAL_INTERVAL = 0.11   # límite oficial: 100 ms
HEAVY_INTERVAL = 0.55     # límite oficial: 500 ms

HEAVY_PATHS = frozenset({
    "/cards/search",
    "/cards/named",
    "/cards/random",
    "/cards/collection",
})

# Duración del bloqueo que aplica Scryfall tras un 429.
RATE_LIMIT_COOLDOWN = 30.0

_RETRY_MAX_ATTEMPTS = 4       # 1 intento inicial + 3 reintentos (red / 5xx)
_RETRY_MAX_429 = 1            # tras un 429 solo se reintenta una vez, ya enfriado
_RETRY_BASE_DELAY = 0.5       # segundos; se dobla en cada reintento
_RETRY_MAX_DELAY = 8.0
_RETRYABLE_5XX = {500, 502, 503, 504}

_COLLECTION_CHUNK = 75        # máximo de identificadores por POST /cards/collection


def _is_heavy(url: str) -> bool:
    """True si la URL (relativa o absoluta) apunta a un endpoint de 2/s."""
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
        # Contadores para diagnóstico (logs / tests).
        self.stats = {"requests": 0, "rate_limited": 0, "deduplicated": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Rate limit
    # ------------------------------------------------------------------

    @property
    def cooling_down(self) -> bool:
        """True mientras dura el bloqueo posterior a un 429."""
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
        """Espera turno respetando el enfriamiento y ambos límites."""
        heavy = _is_heavy(url)
        while True:
            await self._wait_cooldown()
            await self._limiter.acquire(heavy)
            # Si otra petición recibió un 429 mientras esperábamos turno, no
            # salir: esperar a que acabe el bloqueo y pedir turno de nuevo.
            if not self.cooling_down:
                return

    # ------------------------------------------------------------------
    # Transporte
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Ejecuta una petición respetando el rate limit, con reintentos.

        * **429**: activa el enfriamiento global y reintenta una sola vez
          cuando termina. Si vuelve a fallar, se devuelve el 429 al llamador.
        * **5xx transitorios y errores de red**: backoff exponencial con jitter.
        """
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
                    method, url, attempt + 1, _RETRY_MAX_ATTEMPTS, delay, e,
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
                    "(bloqueo de Scryfall).", method, url, wait,
                )
                if rate_limited < _RETRY_MAX_429:
                    rate_limited += 1
                    continue
                return resp

            if resp.status_code in _RETRYABLE_5XX and attempt < _RETRY_MAX_ATTEMPTS - 1:
                delay = self._backoff_delay(attempt)
                log.warning(
                    "Scryfall %s %s → %d (intento %d/%d), reintento en %.1fs",
                    method, url, resp.status_code, attempt + 1, _RETRY_MAX_ATTEMPTS, delay,
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
        base = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)
        return base + random.uniform(0, 0.25)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float:
        try:
            return max(0.0, float(resp.headers.get("Retry-After", "")))
        except (TypeError, ValueError):
            return 0.0

    async def _singleflight(self, key: Any, factory: Callable[[], Awaitable[T]]) -> T:
        """Comparte una única ejecución de ``factory`` entre llamadas concurrentes
        con la misma ``key``.

        La ejecución corre en su propia tarea y cada llamador la espera con
        ``shield``: si uno se cancela (p. ej. se cancela la precarga de un
        mazo), los demás siguen recibiendo el resultado.
        """
        fut = self._inflight.get(key)
        if fut is None:
            fut = asyncio.ensure_future(factory())
            self._inflight[key] = fut

            def _cleanup(f: asyncio.Future[Any]) -> None:
                if self._inflight.get(key) is f:
                    del self._inflight[key]
                if not f.cancelled():
                    f.exception()  # marca la excepción como recuperada

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
        """Recorre las páginas de una lista de Scryfall a partir de la primera."""
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

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def search_all(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Todas las cartas de una búsqueda, recorriendo la paginación."""
        full = {"q": query, **params}
        key = ("search_all", tuple(sorted(full.items())))

        async def run() -> list[dict[str, Any]]:
            first = await self._get_uncached("/cards/search", full)
            return await self._paginate(first)

        return await self._singleflight(key, run)

    async def named(self, name: str, set_code: str | None = None) -> dict[str, Any]:
        """Busca una carta exacta por nombre. Opcional filtro por set."""
        params: dict[str, Any] = {"exact": name}
        if set_code:
            params["set"] = set_code
        return await self._get("/cards/named", params=params)

    async def by_id(self, scryfall_id: str) -> dict[str, Any]:
        return await self._get(f"/cards/{scryfall_id}")

    async def by_set_and_number(
        self, set_code: str, number: str, lang: str | None = None
    ) -> dict[str, Any]:
        """Devuelve una impresión concreta. Si se pasa ``lang``, intenta la
        versión localizada. Devuelve ``{}`` si Scryfall no tiene esa combinación
        (p.ej. la carta no se imprimió en ese idioma)."""
        path = f"/cards/{set_code.lower()}/{number}"
        if lang and lang != "en":
            path = f"{path}/{lang}"
        return await self._get(path)

    async def prints_by_oracle_id(self, oracle_id: str) -> list[dict[str, Any]]:
        """Devuelve todas las impresiones ('unique=prints') de un oracle_id."""
        return await self.search_all(
            f"oracleid:{oracle_id} include:extras",
            unique="prints", order="released", dir="asc",
        )

    async def prints_by_oracle_ids(self, oracle_ids: list[str]) -> list[dict[str, Any]]:
        """Todas las impresiones de varios oracle_ids en una sola búsqueda.

        ``(oracleid:A or oracleid:B …)`` devuelve lo mismo que N llamadas a
        :meth:`prints_by_oracle_id`, pero paginado de 175 en 175: precargar un
        Commander pasa de ~100 búsquedas (≈55 s a 2/s) a unas pocas páginas.
        """
        ids = list(dict.fromkeys(o for o in oracle_ids if o))
        if not ids:
            return []
        if len(ids) == 1:
            return await self.prints_by_oracle_id(ids[0])
        clause = " or ".join(f"oracleid:{o}" for o in ids)
        return await self.search_all(
            f"({clause}) include:extras",
            unique="prints", order="released", dir="asc",
        )

    async def collection(self, identifiers: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Bulk lookup — hasta 75 cartas por request.

        Los identificadores repetidos se envían una sola vez: cada petición a
        este endpoint cuesta medio segundo de cupo.
        """
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
                "POST", "/cards/collection", json={"identifiers": chunk},
            )
            resp.raise_for_status()
            results.extend(resp.json().get("data", []))
        return results

    async def autocomplete(self, query: str) -> list[str]:
        """Devuelve hasta 20 nombres de cartas que empiezan por `query`.

        Durante el enfriamiento posterior a un 429 devuelve ``[]`` al instante
        en vez de dejar colgado el desplegable del buscador 30 segundos.
        """
        query = query.strip()
        if len(query) < 2 or self.cooling_down:
            return []
        payload = await self._get(
            "/cards/autocomplete", params={"q": query, "include_extras": "false"},
        )
        return list(payload.get("data", []) if payload else [])


# Utilidades para extraer info de las respuestas -----------------------------

def is_double_faced(card: dict[str, Any]) -> bool:
    """True si la carta tiene dos caras físicas (DFC/MDFC/transform)."""
    layout = card.get("layout", "normal")
    return layout in {"transform", "modal_dfc", "double_faced_token", "reversible_card"}


def get_face_images(card: dict[str, Any]) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Devuelve (front_uris, back_uris) o (front, None) para cartas normales."""
    if is_double_faced(card):
        faces = card.get("card_faces", [])
        front = faces[0].get("image_uris") if len(faces) > 0 else None
        back = faces[1].get("image_uris") if len(faces) > 1 else None
        return front, back
    return card.get("image_uris"), None


def token_ids_from_card(card: dict[str, Any]) -> list[str]:
    """scryfall_ids de tokens creados por esta carta (según 'all_parts')."""
    tokens = []
    for part in card.get("all_parts", []) or []:
        if part.get("component") == "token":
            tokens.append(part["id"])
    return tokens


def related_parts_from_card(card: dict[str, Any]) -> list[dict[str, str]]:
    """Devuelve todas las partes relacionadas con `component`, `id`, `name`.

    Cubre:
    - Tokens que la carta produce (component="token")
    - Meld result (component="meld_result") — la carta grande resultante
    - Meld parts (component="meld_part") — los dos componentes
    - Combo pieces (component="combo_piece") — parejas tipo Kindred Discovery

    Filtra la propia carta (una carta meld se lista a sí misma como meld_part).
    """
    self_id = card.get("id")
    out = []
    for part in card.get("all_parts", []) or []:
        pid = part.get("id")
        if not pid or pid == self_id:
            continue
        out.append({
            "id": pid,
            "name": part.get("name", ""),
            "component": part.get("component", ""),
            "type_line": part.get("type_line", ""),
        })
    return out
