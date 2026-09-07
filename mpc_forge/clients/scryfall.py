"""Cliente async para Scryfall.

Respeta el rate limit recomendado (~100 ms entre llamadas) y devuelve datos crudos.

Además, reintenta automáticamente en caso de 429 (Too Many Requests) o 5xx
transitorios, honrando la cabecera ``Retry-After`` cuando está presente.
Sin esto, un burst de peticiones (ej. abrir un mazo grande que dispara
``/prints`` en cascada) puede recibir 429 esporádicos y romper la UX
propagando 500s al frontend.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
from mpc_forge.services.rate_limiter import AsyncRateLimiter
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_RATE_LIMIT_INTERVAL = 0.10  # 100 ms entre inicios de llamada — política recomendada por Scryfall.
_RETRY_MAX_ATTEMPTS = 4       # 1 intento inicial + 3 reintentos
_RETRY_BASE_DELAY = 0.5       # segundos; se dobla en cada reintento (exponencial)
_RETRY_MAX_DELAY = 8.0        # tope duro para no colgar la request eternamente
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ScryfallClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=SCRYFALL_API,
            headers={
                "User-Agent": SCRYFALL_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=30.0,
            verify=not ssl_insecure(),
        )
        self._limiter = AsyncRateLimiter(_RATE_LIMIT_INTERVAL)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Ejecuta una request pasando por el rate limiter y con reintentos.

        Reintenta en 429 y 5xx transitorios (500/502/503/504). Para 429 usa
        primero el ``Retry-After`` de la respuesta si está; si no, backoff
        exponencial con jitter. Para 5xx, backoff sin honrar cabeceras.

        Cualquier error de red (``httpx.RequestError``) también se reintenta.

        Al agotar reintentos, propaga el último status vía ``raise_for_status``
        o la última excepción de red.
        """
        last_exc: Exception | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            # Rate limiter global antes de cada intento (también antes de reintentos)
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, params=params, json=json)
            except httpx.RequestError as e:
                last_exc = e
                delay = self._backoff_delay(attempt)
                log.warning(
                    "Scryfall %s %s falló por red (intento %d/%d), reintento en %.1fs: %s",
                    method, url, attempt + 1, _RETRY_MAX_ATTEMPTS, delay, e,
                )
                await asyncio.sleep(delay)
                continue

            # ¿Status reintentable?
            if resp.status_code in _RETRYABLE_STATUS and attempt < _RETRY_MAX_ATTEMPTS - 1:
                delay = self._retry_delay_from_response(resp, attempt)
                log.warning(
                    "Scryfall %s %s → %d (intento %d/%d), reintento en %.1fs",
                    method, url, resp.status_code, attempt + 1, _RETRY_MAX_ATTEMPTS, delay,
                )
                await resp.aread()  # drenar el body para liberar la conexión
                await asyncio.sleep(delay)
                continue

            return resp

        # Se agotaron los reintentos por errores de red
        if last_exc is not None:
            raise last_exc
        # No debería llegar aquí — el loop siempre devuelve o lanza
        raise RuntimeError("Scryfall: reintentos agotados sin respuesta")

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """Delay exponencial con jitter para reintentos por red."""
        base = min(_RETRY_BASE_DELAY * (2 ** attempt), _RETRY_MAX_DELAY)
        return base + random.uniform(0, 0.25)

    @staticmethod
    def _retry_delay_from_response(resp: httpx.Response, attempt: int) -> float:
        """Delay para reintentar tras 429/5xx.

        Prioriza la cabecera ``Retry-After`` si Scryfall la envía. Si no,
        usa backoff exponencial. En ambos casos aplica el tope ``_RETRY_MAX_DELAY``.
        """
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _RETRY_MAX_DELAY)
            except (TypeError, ValueError):
                pass
        return ScryfallClient._backoff_delay(attempt)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._request_with_retry("GET", path, params=params)
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        return resp.json()

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
        results: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "q": f"oracleid:{oracle_id} include:extras",
            "unique": "prints",
            "order": "released",
            "dir": "asc",
        }
        page = await self._get("/cards/search", params=params)
        while page:
            data = page.get("data", [])
            if not data:
                break
            results.extend(data)
            if not page.get("has_more"):
                break
            next_url = page.get("next_page")
            if not next_url:
                break
            resp = await self._request_with_retry("GET", next_url)
            resp.raise_for_status()
            page = resp.json()
        return results

    async def collection(self, identifiers: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Bulk lookup — hasta 75 cartas por request."""
        results: list[dict[str, Any]] = []
        for i in range(0, len(identifiers), 75):
            chunk = identifiers[i : i + 75]
            resp = await self._request_with_retry(
                "POST", "/cards/collection", json={"identifiers": chunk},
            )
            resp.raise_for_status()
            payload = resp.json()
            results.extend(payload.get("data", []))
        return results

    async def autocomplete(self, query: str) -> list[str]:
        """Devuelve hasta 20 nombres de cartas que empiezan por `query`.

        Endpoint público de Scryfall: /cards/autocomplete?q=…
        """
        query = query.strip()
        if len(query) < 2:
            return []
        payload = await self._get("/cards/autocomplete", params={"q": query, "include_extras": "false"})
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
