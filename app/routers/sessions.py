"""M1：会话/消息 API（本地 SQLite）。

URL 与前端约定：
  GET    /api/sessions                         会话列表（enabled=false 时返回空列表）
  POST   /api/sessions                         新建会话
  GET    /api/sessions/status                  数据库/索引状态
  POST   /api/sessions/import                  导入旧 localStorage（幂等）
  GET    /api/sessions/{id}                    会话详情
  GET    /api/sessions/{id}/messages           消息列表（按 seq 升序）
  PATCH  /api/sessions/{id}                    改标题/主题/归档
  DELETE /api/sessions/{id}                    软删除；?purge=true 彻底删除
  DELETE /api/sessions/{id}/messages           清空会话消息
  POST   /api/sessions/{id}/end                会话结束标记（M2 起触发补充提取）
  GET    /api/sessions/{id}/export             导出 JSON

边界：
- 不记录消息正文到日志；
- `MEMORY_ENABLED=off` 时列表返回 enabled=false，写接口返回 503；
- 管理写接口的本机令牌校验在 M5 接入（当前与聊天同源，避免破坏局域网聊天）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.chat_access import require_chat_access
from app.core.config import get_settings
from app.memory import runtime
from app.memory.store import MemoryStore, SessionNotFound
from app.memory.util import iso_to_ms
from app.models.schemas import SessionCreate, SessionImport, SessionUpdate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/sessions",
    tags=["sessions"],
    # 阶段 0：局域网访问会话/导出必须带口令；本机自动放行。
    dependencies=[Depends(require_chat_access)],
)


# ============================================================
# 基础
# ============================================================

def _store() -> MemoryStore:
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


def _session_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "topic_id": row.get("topic_id"),
        "status": row.get("status") or "active",
        "message_count": int(row.get("message_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "created_ts": iso_to_ms(row.get("created_at")),
        "updated_ts": iso_to_ms(row.get("updated_at")),
        "last_message_at": row.get("last_message_at"),
        "last_message_ts": iso_to_ms(row.get("last_message_at")),
    }


def _message_item(row: Dict[str, Any]) -> Dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    role = "ai" if row.get("role") == "assistant" else str(row.get("role") or "ai")
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "seq": int(row.get("seq") or 0),
        "role": role,
        "text": row.get("content") or "",
        "ts": iso_to_ms(row.get("created_at")),
        "created_at": row.get("created_at"),
        "status": row.get("status") or "complete",
        "source": row.get("source") or "",
        "model": row.get("model") or "",
        "references": row.get("references") if isinstance(row.get("references"), list) else [],
        "generation": meta.get("generation") if isinstance(meta.get("generation"), dict) else None,
        "retry_upstream": meta.get("retry_upstream") is True,
        "upstream_retry": meta.get("upstream_retry") is True,
        # 工程标准：可观测本轮是否带入上下文、是否无检索、是否 bypass。
        "context_status": str(meta.get("context_status") or ""),
        "context_reason": str(meta.get("context_reason") or ""),
        "no_context": meta.get("no_context") is True,
        "bypass": meta.get("bypass") is True,
        "memory_trace": (
            meta.get("memory_trace")
            if isinstance(meta.get("memory_trace"), dict)
            else None
        ),
    }


# ============================================================
# 只读
# ============================================================

@router.get("")
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query("active", max_length=16),
) -> Dict[str, Any]:
    if runtime.get_store() is None:
        return {"enabled": False, "sessions": [], "status": runtime.status()}
    store = _store()
    try:
        rows = store.list_sessions(limit=limit, offset=offset, status=status)
    except Exception as exc:  # noqa: BLE001 - 读取失败按服务不可用返回
        logger.warning("会话列表读取失败：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_unavailable", "message": "会话库暂时不可用"},
        ) from exc
    return {
        "enabled": True,
        "sessions": [_session_item(row) for row in rows],
        "status": runtime.status(),
    }


@router.get("/status")
def session_status() -> Dict[str, Any]:
    return runtime.status()


@router.get("/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    store = _store()
    row = store.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "会话不存在"})
    return {"session": _session_item(row)}


@router.get("/{session_id}/messages")
def list_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=1000),
    before_seq: Optional[int] = Query(None, ge=1),
) -> Dict[str, Any]:
    store = _store()
    try:
        rows = store.list_messages(session_id, limit=limit, before_seq=before_seq)
    except SessionNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    return {
        "session_id": session_id,
        "messages": [_message_item(row) for row in rows],
    }


@router.get("/{session_id}/export")
def export_session(session_id: str) -> Dict[str, Any]:
    store = _store()
    data = store.export_session(session_id)
    if data is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    return {
        "enabled": True,
        "session": _session_item(data["session"]),
        "messages": [_message_item(row) for row in data["messages"]],
    }


# ============================================================
# 写入
# ============================================================

@router.post("")
def create_session(payload: SessionCreate = SessionCreate()) -> Dict[str, Any]:
    store = _store()
    try:
        row = store.create_session(title=payload.title, topic_id=payload.topic_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("新建会话失败：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_unavailable", "message": "会话库暂时不可用"},
        ) from exc
    return {"enabled": True, "session": _session_item(row)}


@router.patch("/{session_id}")
def update_session(session_id: str, payload: SessionUpdate) -> Dict[str, Any]:
    store = _store()
    try:
        row = store.update_session(
            session_id,
            title=payload.title,
            topic_id=payload.topic_id,
            status=payload.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid", "message": str(exc)})
    if row is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    return {"enabled": True, "session": _session_item(row)}


@router.delete("/{session_id}")
def delete_session(
    session_id: str,
    purge: bool = Query(False, description="true = 彻底删除（不可恢复）"),
) -> Dict[str, Any]:
    store = _store()
    ok = store.delete_session(session_id, purge=purge)
    if not ok:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    return {"enabled": True, "ok": True, "purged": bool(purge)}


@router.delete("/{session_id}/messages")
def clear_messages(session_id: str) -> Dict[str, Any]:
    store = _store()
    try:
        deleted = store.clear_session_messages(session_id)
    except SessionNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    return {"enabled": True, "ok": True, "deleted": deleted}


@router.post("/{session_id}/end")
def end_session(session_id: str) -> Dict[str, Any]:
    """会话结束标记。

    M1 只更新 updated_at；M2 会在这里为上一会话入队补充记忆提取。
    """
    store = _store()
    if not store.touch_session(session_id):
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "会话不存在"}
        )
    queued = False
    try:
        cfg = get_settings()
        if cfg.memory_enabled and cfg.memory_auto_extract:
            store.jobs.enqueue(
                kind="extract_memory",
                session_id=session_id,
                dedupe_key=f"extract_memory:{session_id}",
                priority=1,
                delay_seconds=0,
                payload={"session_id": session_id, "reason": "session_end"},
            )
            queued = True
    except Exception as exc:  # noqa: BLE001 - 入队失败不能影响会话结束标记
        logger.warning("会话结束入队失败（忽略）：%s", type(exc).__name__)
    return {"enabled": True, "ok": True, "extraction_queued": queued}


@router.post("/import")
def import_sessions(payload: SessionImport) -> Dict[str, Any]:
    store = _store()
    try:
        result = store.import_sessions(payload.sessions)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        logger.warning("旧会话导入失败：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_unavailable", "message": "会话库暂时不可用"},
        ) from exc
    return {"enabled": True, **result}


__all__ = ["router"]
