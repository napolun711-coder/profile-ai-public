"""GET /web：返回前端页面（Phase 2 从根目录 main.py 原样迁移）。"""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.paths import STATIC_DIR

router = APIRouter()


@router.get("/web")
def web():

    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )

@router.get("/memory")
def memory_page():
    """M5：记忆管理页（数据由 /api/memory/* 提供；写操作仅限本机）。"""
    return FileResponse(
        STATIC_DIR / "memory.html",
        headers={"Cache-Control": "no-cache"},
    )
