"""阶段 2B：资料集 / 事项（topics）作用域 API。

边界：
- topic 是记忆与会话的作用域，不是长期记忆本身；
- 保留现有 SQLite 权威源，不引入新服务；
- 本机放行；局域网需要阶段 0 口令。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.chat_access import require_chat_access, require_local_access
from app.memory import runtime
from app.models.schemas import TopicCreate, TopicUpdate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/topics",
    tags=["topics"],
    dependencies=[Depends(require_chat_access)],
)


def _store():
    store = runtime.get_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "memory_disabled",
                "message": "对话持久化未启用（MEMORY_ENABLED=off 或初始化失败）",
            },
        )
    return store


@router.get("")
def list_topics(
    status: str = Query("active", max_length=16),
    limit: int = Query(200, ge=1, le=500),
) -> Dict[str, Any]:
    if runtime.get_store() is None:
        return {"enabled": False, "topics": []}
    return {"enabled": True, "topics": _store().list_topics(status=status, limit=limit)}


@router.post("", dependencies=[Depends(require_local_access)])
def create_topic(payload: TopicCreate) -> Dict[str, Any]:
    store = _store()
    try:
        topic = store.create_topic(
            name=payload.name,
            kind=payload.kind,
            description=payload.description,
            parent_id=payload.parent_id or None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_topic", "message": str(exc)}
        ) from exc
    return {"enabled": True, "topic": topic}


@router.get("/{topic_id}")
def get_topic(topic_id: str) -> Dict[str, Any]:
    topic = _store().get_topic(topic_id)
    if topic is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "topic_not_found", "message": "找不到这个资料集"},
        )
    return {"enabled": True, "topic": topic}


@router.patch("/{topic_id}", dependencies=[Depends(require_local_access)])
def update_topic(topic_id: str, payload: TopicUpdate) -> Dict[str, Any]:
    try:
        topic = _store().update_topic(
            topic_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_topic", "message": str(exc)}
        ) from exc
    if topic is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "topic_not_found", "message": "找不到这个资料集"},
        )
    return {"enabled": True, "topic": topic}
