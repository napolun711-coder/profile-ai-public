"""收件箱接口（Phase 16 P3）：`data\\inbox\\` 的轮询状态与「立即扫描」。

- `GET  /api/inbox/status`  轮询状态（开关、目录、计数、最近事件）——只读，局域网可看
- `POST /api/inbox/scan`    立刻扫一次（平时后台线程自己按周期扫）——仅本机 + 令牌

为什么「立即扫描」要本机 + 令牌：它会让收件箱里的文件**马上进入入库流水线**
（可能一路走到知识库）。这是会动数据的动作，权限档与删除一致（`control.guard`），
而不是只读那档（`guard_remote`）—— 手机页面看不到这个按钮，硬调也是 403。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request

from app.models.schemas import InboxScan
from app.routers import control
from app.services import inbox as inbox_service

router = APIRouter(tags=["inbox"])

logger = logging.getLogger(__name__)


@router.get("/api/inbox/status")
def inbox_status(request: Request) -> Dict[str, Any]:
    """收件箱轮询状态（页面刷新时读一次）。"""
    control.guard_remote(request)
    return {"success": True, **inbox_service.status()}


@router.post("/api/inbox/scan")
def inbox_scan(payload: InboxScan, request: Request) -> Dict[str, Any]:
    """立刻扫一次收件箱（用户丢完文件不想等下一轮时用）。"""
    control.guard_action(request, payload.token)
    result = inbox_service.scan_once(reason=payload.reason or "manual")
    logger.info(
        "手动扫描收件箱：受理 %s，重复 %s，失败 %s",
        result.get("accepted"),
        result.get("duplicate"),
        result.get("failed"),
    )
    return {
        "success": True,
        "message": (
            f"扫描完成：受理 {result.get('accepted', 0)} 个、"
            f"重复 {result.get('duplicate', 0)} 个、失败 {result.get('failed', 0)} 个"
            f"（还在等长稳的 {len(result.get('settling') or [])} 个下一轮再看）"
        ),
        "result": result,
        "status": inbox_service.status(),
    }
