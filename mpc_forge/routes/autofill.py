from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from mpc_forge import config as cfg
from mpc_forge.services.printing import mpc_autofill

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["integrations"])


class AutofillStatusResponse(BaseModel):
    available: bool
    exe_path: str | None = None
    source: str
    hint: str | None = None


class AutofillLaunchRequest(BaseModel):
    xml_filename: str


@router.get("/mpc-autofill/status", response_model=AutofillStatusResponse)
async def autofill_status() -> AutofillStatusResponse:
    st = mpc_autofill.detect()
    hint = None
    if not st.available:
        hint = (
            "Descarga el binario desde github.com/chilli-axe/mpc-autofill/releases "
            "y colócalo en la carpeta del proyecto (o configura su ruta en Ajustes)."
        )
    return AutofillStatusResponse(
        available=st.available,
        exe_path=st.exe_path,
        source=st.source,
        hint=hint,
    )


@router.post("/mpc-autofill/launch")
async def autofill_launch(payload: AutofillLaunchRequest) -> dict[str, Any]:
    exports_dir = Path(cfg.PATHS.exports_dir).resolve()
    xml_path = (exports_dir / payload.xml_filename).resolve()

    try:
        xml_path.relative_to(exports_dir)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ruta inválida") from None

    if not xml_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"XML no encontrado: {payload.xml_filename}")

    try:
        pid = mpc_autofill.launch(xml_path)
    except RuntimeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"launched": True, "pid": pid, "xml_path": str(xml_path)}
