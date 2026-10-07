from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


def is_benign_connection_reset(context: dict[str, Any]) -> bool:
    exc = context.get("exception")
    if not isinstance(exc, ConnectionResetError):
        return False
    where = f"{context.get('handle', '')} {context.get('message', '')}"
    return "_call_connection_lost" in where


def silence_windows_connection_resets(loop: asyncio.AbstractEventLoop) -> None:
    if sys.platform != "win32":
        return
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if is_benign_connection_reset(context):
            log.debug("Conexión cerrada por el cliente: %s", context.get("exception"))
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
