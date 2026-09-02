"""CubeCobra — export plaintext oficial.

CubeCobra ofrece un endpoint público de descarga en texto plano por cubo,
con parámetros para agrupar por categoría. Devuelve las cartas una por línea.
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


class CubeCobraSite(ImportSite):
    key: ClassVar[str] = "cubecobra"
    name: ClassVar[str] = "CubeCobra"
    host_names: ClassVar[tuple[str, ...]] = ("cubecobra.com", "www.cubecobra.com")
    example_url: ClassVar[str] = "https://cubecobra.com/cube/list/…"
    base_url: ClassVar[str] = "cubecobra.com"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        # Path típico: /cube/list/{cube_id} o /cube/overview/{cube_id}
        path = urlparse(url).path or ""
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise InvalidURLError(url)
        cube_id = parts[-1]
        resp = await cls.request(
            f"/cube/download/plaintext/{cube_id}?{_DOWNLOAD_QUERY}"
        )
        text = resp.text or ""
        # CubeCobra emite líneas como `# mainboard` que rompen el parseo — las quitamos.
        cleaned = "\n".join(
            line for line in text.splitlines() if not line.startswith("# ")
        )
        return cleaned.strip()
