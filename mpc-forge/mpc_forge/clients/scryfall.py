"""Cliente async para Scryfall.

Respeta el rate limit recomendado (~100 ms entre llamadas) y devuelve datos crudos.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from mpc_forge.config import SCRYFALL_API, SCRYFALL_USER_AGENT
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_RATE_LIMIT_SLEEP = 0.10  # 100 ms entre llamadas — política recomendada por Scryfall.


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
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            await asyncio.sleep(_RATE_LIMIT_SLEEP)
            resp = await self._client.get(path, params=params)
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

    async def by_set_and_number(self, set_code: str, number: str) -> dict[str, Any]:
        return await self._get(f"/cards/{set_code.lower()}/{number}")

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
            async with self._lock:
                await asyncio.sleep(_RATE_LIMIT_SLEEP)
                resp = await self._client.get(next_url)
            resp.raise_for_status()
            page = resp.json()
        return results

    async def collection(self, identifiers: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Bulk lookup — hasta 75 cartas por request."""
        results: list[dict[str, Any]] = []
        for i in range(0, len(identifiers), 75):
            chunk = identifiers[i : i + 75]
            async with self._lock:
                await asyncio.sleep(_RATE_LIMIT_SLEEP)
                resp = await self._client.post(
                    "/cards/collection", json={"identifiers": chunk}
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
