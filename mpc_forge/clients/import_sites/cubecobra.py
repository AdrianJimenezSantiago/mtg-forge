"""CubeCobra — export plaintext oficial + nombre desde JSON API.

CubeCobra ofrece:
  - Descarga en texto plano: ``/cube/download/plaintext/{cube_id}``
  - Metadatos JSON del cubo: ``/cube/api/cubejson/{cube_id}`` → ``{"name": "…"}``
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError

_DOWNLOAD_QUERY = (
    "primary=Color%20Category"
    "&secondary=Types-Multicolor"
    "&tertiary=Mana%20Value"
    "&quaternary=Alphabetical"
    "&showother=false"
)


def _extract_cube_id(url: str) -> str | None:
    """Extrae el cube_id del último segmento de la ruta.

    Soporta:
      ``/cube/list/{cube_id}``
      ``/cube/overview/{cube_id}``
      ``/cube/playtest/{cube_id}``
    """
    path = urlparse(url).path or ""
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else None


class CubeCobraSite(ImportSite):
    key: ClassVar[str] = "cubecobra"
    name: ClassVar[str] = "CubeCobra"
    host_names: ClassVar[tuple[str, ...]] = ("cubecobra.com", "www.cubecobra.com")
    example_url: ClassVar[str] = "https://cubecobra.com/cube/list/…"
    base_url: ClassVar[str] = "cubecobra.com"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        cube_id = _extract_cube_id(url)
        if not cube_id:
            raise InvalidURLError(url)
        resp = await cls.request(
            f"/cube/download/plaintext/{cube_id}?{_DOWNLOAD_QUERY}"
        )
        text = resp.text or ""
        # CubeCobra emite líneas como `# mainboard` que rompen el parseo.
        cleaned = "\n".join(
            line for line in text.splitlines() if not line.startswith("# ")
        )
        return cleaned.strip()

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        """Nombre del cubo desde la API JSON de CubeCobra.

        Endpoint: ``/cube/api/cubejson/{cube_id}`` → ``{"name": "…", …}``.
        Llamada independiente (no cacheable junto a retrieve_card_list porque
        el texto plano y el JSON son endpoints distintos).
        """
        cube_id = _extract_cube_id(url)
        if not cube_id:
            return None
        try:
            resp = await cls.request(f"/cube/api/cubejson/{cube_id}")
            payload = resp.json()
            name = (payload.get("name") or "").strip()
            return name if name else None
        except Exception:  # noqa: BLE001
            return None
