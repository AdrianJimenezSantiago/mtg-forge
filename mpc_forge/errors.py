from __future__ import annotations

import string
from typing import Any

from fastapi import HTTPException

from mpc_forge.services.system.i18n import get_translations

ERROR_KEY_PREFIX = "error_"


class ApiError(HTTPException):
    """Error de la API con un código estable que el cliente puede traducir.

    El detalle viaja como `{"code", "params", "message"}`; `message` se rellena
    al responder con la traducción del idioma de la petición.
    """

    def __init__(self, status_code: int, code: str, **params: Any) -> None:
        super().__init__(status_code, detail={"code": code, "params": params})
        self.code = code
        self.params = params


class CodedError(Exception):
    """Excepción de servicio que lleva un código traducible y sus parámetros."""

    code: str = "error"

    def __init__(self, message: str = "", *, code: str | None = None, **params: Any) -> None:
        super().__init__(message or (code or self.code))
        if code is not None:
            self.code = code
        self.params = params


def from_exception(status_code: int, exc: Exception, fallback_code: str) -> ApiError:
    code = getattr(exc, "code", None) or fallback_code
    params = dict(getattr(exc, "params", None) or {})
    if not getattr(exc, "code", None):
        params.setdefault("reason", str(exc))
    return ApiError(status_code, code, **params)


class _SafeParams(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format(template: str, params: dict[str, Any]) -> str:
    try:
        return string.Formatter().vformat(template, (), _SafeParams(params))
    except (ValueError, IndexError):
        return template


def translate_message(lang: str, code: str, params: dict[str, Any] | None = None) -> str | None:
    template = get_translations(lang).get(ERROR_KEY_PREFIX + code)
    if not template:
        return None
    return _format(template, params or {})


def localize_detail(detail: Any, lang: str) -> Any:
    if not isinstance(detail, dict) or not isinstance(detail.get("code"), str):
        return detail
    params = detail.get("params") or {}
    message = translate_message(lang, detail["code"], params)
    if message is None:
        message = detail.get("message") or params.get("reason") or detail["code"]
    return {**detail, "message": message}
