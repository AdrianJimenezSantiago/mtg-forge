"""Endpoints de debug: acceso al log temporal para diagnóstico."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from mpc_forge.services import logging_setup

router = APIRouter(prefix="/api/debug", tags=["debug"])


class LogInfoResponse(BaseModel):
    exists: bool
    path: str | None
    size_bytes: int = 0


@router.get("/log/info", response_model=LogInfoResponse)
async def log_info() -> LogInfoResponse:
    """Info del log actual (existe, ruta, tamaño)."""
    p = logging_setup.current_log_path()
    if p is None or not p.exists():
        return LogInfoResponse(exists=False, path=None)
    return LogInfoResponse(
        exists=True,
        path=str(p),
        size_bytes=p.stat().st_size,
    )


@router.get("/log/tail", response_class=PlainTextResponse)
async def log_tail(n: int = 200) -> str:
    """Últimas N líneas del log, en texto plano."""
    n = max(1, min(n, 5000))
    return logging_setup.read_tail(n_lines=n)


@router.get("/log/download")
async def log_download():
    """Descarga el log completo como archivo .txt para compartirlo."""
    p = logging_setup.current_log_path()
    if p is None or not p.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay log activo")
    # Nombre con timestamp para poder guardar varios
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"mpc-forge-{ts}.log"
    return FileResponse(
        path=str(p),
        media_type="text/plain; charset=utf-8",
        filename=filename,
    )
