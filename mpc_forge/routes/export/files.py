from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from mpc_forge import config as cfg
from mpc_forge.models import PrintRun, PrintRunItem
from mpc_forge.routes.dependencies import DbDep
from mpc_forge.routes.export._shared import make_router
from mpc_forge.services.system import backup as backup_service
from mpc_forge.services.system import storage as storage_service

router = make_router()

_EXPORT_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "zip": "application/zip",
    "txt": "text/plain; charset=utf-8",
    "xml": "application/xml",
}


@router.get("/exports/{filename}")
async def download_export(filename: str) -> FileResponse:
    exports_dir = cfg.PATHS.exports_dir
    target = (exports_dir / filename).resolve()
    if not target.exists() or exports_dir not in target.parents:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Archivo no encontrado")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    media = _EXPORT_MEDIA_TYPES.get(ext, "application/octet-stream")
    return FileResponse(target, media_type=media, filename=filename)


class PrintRunView(BaseModel):
    id: int
    name: str
    created_at: datetime
    cardstock: str
    total_cards: int
    tier_size: int
    estimated_cost_eur: float
    xml_path: str | None
    item_count: int


@router.get("/runs", response_model=list[PrintRunView])
async def list_runs(db: DbDep) -> list[PrintRunView]:
    item_counts = (
        select(PrintRunItem.run_id, func.count(PrintRunItem.id).label("n"))
        .group_by(PrintRunItem.run_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(PrintRun, func.coalesce(item_counts.c.n, 0))
            .outerjoin(item_counts, item_counts.c.run_id == PrintRun.id)
            .order_by(PrintRun.created_at.desc())
        )
    ).all()
    return [
        PrintRunView(
            id=r.id,
            name=r.name,
            created_at=r.created_at,
            cardstock=r.cardstock,
            total_cards=r.total_cards,
            tier_size=r.tier_size,
            estimated_cost_eur=r.estimated_cost_eur,
            xml_path=r.xml_path,
            item_count=int(count),
        )
        for r, count in rows
    ]


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(run_id: int, db: DbDep) -> None:
    run = await db.get(PrintRun, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    await db.delete(run)
    await db.commit()


class BackupResponse(BaseModel):
    path: str
    size_bytes: int


@router.post("/backup", response_model=BackupResponse)
async def create_backup_endpoint() -> BackupResponse:
    zip_path = backup_service.create_backup()
    storage_service.invalidate()
    return BackupResponse(path=str(zip_path), size_bytes=zip_path.stat().st_size)
