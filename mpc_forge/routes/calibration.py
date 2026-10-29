from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mpc_forge.errors import ApiError
from mpc_forge.services.printing import calibration

log = logging.getLogger(__name__)

router = APIRouter(tags=["library"])


class CalibrationRequest(BaseModel):
    measured_x_mm: float = Field(..., ge=-50, le=50)
    measured_y_mm: float = Field(..., ge=-50, le=50)
    flip_edge: Literal["long", "short"] = "long"


class ScaleCalibrationRequest(BaseModel):
    measured_x_mm: float = Field(..., gt=0, le=400)
    measured_y_mm: float = Field(..., gt=0, le=400)
    current_x_pct: float = Field(
        100.0, ge=calibration.PRINT_SCALE_RANGE_PCT[0], le=calibration.PRINT_SCALE_RANGE_PCT[1]
    )
    current_y_pct: float = Field(
        100.0, ge=calibration.PRINT_SCALE_RANGE_PCT[0], le=calibration.PRINT_SCALE_RANGE_PCT[1]
    )


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
        raise ApiError(status.HTTP_400_BAD_REQUEST, "unsupported_page_size")
    path = await asyncio.to_thread(
        calibration.build_sheet, None, page_size=page_size, flip_edge=flip_edge
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="calibracion-duplex.pdf",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/calibration/derive-scale")
async def derive_scale(payload: ScaleCalibrationRequest) -> dict[str, Any]:
    return calibration.derive_scale(
        payload.measured_x_mm,
        payload.measured_y_mm,
        current_x_pct=payload.current_x_pct,
        current_y_pct=payload.current_y_pct,
    ).to_dict()


@router.get("/api/calibration/scale-sheet")
async def scale_sheet(
    page_size: str = Query("a4"),
    scale_x_pct: float = Query(100.0),
    scale_y_pct: float = Query(100.0),
) -> FileResponse:
    if page_size.lower() not in ("a4", "letter"):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "unsupported_page_size")
    path = await asyncio.to_thread(
        calibration.build_scale_sheet,
        None,
        page_size=page_size,
        scale_x_pct=scale_x_pct,
        scale_y_pct=scale_y_pct,
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename="calibracion-escala.pdf",
        headers={"Cache-Control": "no-store"},
    )
