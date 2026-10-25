"""M4-mini：只读历史检索（不写记忆、不自动注入 prompt）。"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.core.chat_access import require_chat_access
from app.memory import runtime

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/history",
    tags=["history"],
    dependencies=[Depends(require_chat_access)],
)


@router.get("/search")
def search_history(
    q: str = Query("", max_length=200),
    topic_id: str = Query("", max_length=128),
    limit: int = Query(10, ge=1, le=20),
) -> Dict[str, Any]:
    """只读检索历史消息；返回有界片段，不返回整段会话。"""
    text = " ".join(str(q or "").split())
    if len(text) < 2:
        return {"enabled": True, "items": [], "count": 0, "error": "query_too_short"}
    store = runtime.get_store()
    if store is None:
        return {"enabled": False, "items": [], "count": 0}
    try:
        items = store.search_history(text, topic_id=topic_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 检索失败不能让页面崩
        logger.warning("历史检索失败：%s", type(exc).__name__)
        return {"enabled": True, "items": [], "count": 0, "error": "search_failed"}
    return {"enabled": True, "items": items, "count": len(items)}
