from __future__ import annotations

import re
from typing import ClassVar
from urllib.parse import urlparse

from .base import ImportSite, InvalidURLError, _slug_to_title

_DECKSTATS_PATH_RE = re.compile(r"^/decks/(\d+)/(\d+)-(.+)$")
_DECKSTATS_PATH_BARE_RE = re.compile(r"^/decks/\d+/\d+")


class DeckstatsSite(ImportSite):
    key: ClassVar[str] = "deckstats"
    name: ClassVar[str] = "Deckstats"
    host_names: ClassVar[tuple[str, ...]] = ("deckstats.net", "www.deckstats.net")
    example_url: ClassVar[str] = "https://deckstats.net/decks/1234/5678-my-deck"
    base_url: ClassVar[str] = "deckstats.net"

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        path = urlparse(url).path or ""
        if not _DECKSTATS_PATH_BARE_RE.match(path):
            raise InvalidURLError(url)
        resp = await cls.request(f"{path}?export_txt=1")
        text = resp.text or ""
        text = re.sub(r"^\s*//\s*", "//", text, flags=re.MULTILINE)
        return text.strip()

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        path = urlparse(url).path or ""
        m = _DECKSTATS_PATH_RE.match(path)
        if not m:
            return None
        slug = m.group(3).strip()
        title = _slug_to_title(slug)
        return title if title else None
