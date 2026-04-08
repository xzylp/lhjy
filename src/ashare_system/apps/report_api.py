"""报告查询 API"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException


def build_router(reports_dir: Path | None = None) -> APIRouter:
    router = APIRouter(prefix="/reports", tags=["reports"])
    _dir = reports_dir or Path("logs/reports")

    @router.get("/")
    async def list_reports():
        if not _dir.exists():
            return {"reports": []}
        files = sorted(_dir.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
        return {"reports": [f.name for f in files[:20]]}

    @router.get("/{filename}")
    async def get_report(filename: str):
        path = _dir / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"报告不存在: {filename}")
        return {"filename": filename, "content": path.read_text(encoding="utf-8")}

    return router
