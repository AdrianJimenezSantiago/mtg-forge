from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mpc_forge.services.printing import calibration

log = logging.getLogger(__name__)

router = APIRouter(tags=["library"])


class CalibrationRequest(BaseModel):
    measured_x_mm: float = Field(..., ge=-50, le=50)
    measured_y_mm: float = Field(..., ge=-50, le=50)
    flip_edge: Literal["long", "short"] = "long"


@router.post("/api/calibration/derive")
async def derive_calibration(payload: CalibrationRequest) -> dict[str, Any]:
    result = calibration.derive_offsets(
        payload.measured_x_mm,
        payload.measured_y_mm,
        flip_edge=payload.flip_edge,
    )
    return calibration.explain(result)


@router.get("/api/calibration/sheet")
async def calibration_sheet(
    page_size: str = Query("a4"),
    flip_edge: Literal["long", "short"] = "long",
) -> FileResponse:
    if page_size.lower() not in ("a4", "letter"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tamaño de página no soportado")
    path = await asyncio.to_thread(
        calibration.build_sheet, None, page_size=page_size, flip_edge=flip_edge
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="calibracion-duplex.pdf",
        headers={"Cache-Control": "no-store"},
    )
