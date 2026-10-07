from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from mpc_forge import config as cfg
from mpc_forge.ssl_config import ssl_insecure

log = logging.getLogger(__name__)

_REGISTRY: list[type[ImportSite]] = []


def get_registry() -> list[type[ImportSite]]:
    return list(_REGISTRY)


class ImportSiteError(RuntimeError):
    pass


class InvalidURLError(ImportSiteError):
    def __init__(self, url: str) -> None:
        super().__init__(f"URL no válida para este sitio: {url}")
        self.url = url


def _slug_to_title(slug: str) -> str:
    words = slug.replace("-", " ").replace("_", " ").split()
    if not words or all(w.isdigit() for w in words):
        return ""
    return " ".join(w.capitalize() for w in words)


class ImportSite:
    key: ClassVar[str] = ""
    name: ClassVar[str] = ""
    host_names: ClassVar[tuple[str, ...]] = ()
    example_url: ClassVar[str] = ""
    base_url: ClassVar[str] = ""

    request_timeout: ClassVar[float] = 30.0

    def __init_subclass__(cls, /, register: bool = True, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not register:
            return
        if not cls.key:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `key`")
        if not cls.name:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `name`")
        if not cls.host_names:
            raise TypeError(f"ImportSite subclass {cls.__name__} lacks `host_names`")
        _REGISTRY.append(cls)

    @classmethod
    def get_headers(cls) -> dict[str, str]:
        return {
            "User-Agent": cfg.MOXFIELD_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }

    @classmethod
    async def request(
        cls,
        path: str,
        *,
        netloc: str | None = None,
        headers: dict[str, str] | None = None,
        scheme: str = "https",
    ) -> httpx.Response:
        merged_headers = {**cls.get_headers(), **(headers or {})}
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            host = netloc or cls.base_url
            if not host:
                raise ImportSiteError(
                    f"{cls.__name__}: no se puede construir URL sin base_url ni netloc"
                )
            if "://" not in host:
                host = f"{scheme}://{host}"
            host = host.rstrip("/")
            path_clean = path if path.startswith("/") else f"/{path}"
            url = f"{host}{path_clean}"

        async with httpx.AsyncClient(
            timeout=cls.request_timeout,
            follow_redirects=True,
            verify=not ssl_insecure(),
            headers=merged_headers,
        ) as client:
            log.debug("[%s] GET %s", cls.key, url)
            resp = await client.get(url)
            resp.raise_for_status()
            return resp

    @classmethod
    async def retrieve_card_list(cls, url: str) -> str:
        raise NotImplementedError

    @classmethod
    async def retrieve_deck_name(cls, url: str) -> str | None:
        return None
