"""M2：长期记忆管理 API（手动 CRUD / 确认 / 拒绝 / 来源 / 事件 / 状态）。

边界：
- `MEMORY_ENABLED=off` 时读接口返回 enabled=false，写接口 503；
- 不记录记忆正文到日志；
- 本机令牌保护按设计在 M5 接入（当前与聊天同源）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.core.chat_access import require_chat_access, require_local_access
from app.core.config import get_settings
from app.memory import runtime
from app.memory.memories import MemoryConflict, MemoryNotFound, MemoryRepository
from app.memory.store import MemoryStore
from app.memory.util import iso_to_ms
from app.models.schemas import MemoryCreate, MemoryUpdate

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    # 阶段 0：局域网读取记忆/来源/事件同样需要口令；本机自动放行。
    dependencies=[Depends(require_chat_access)],
)


def _store() -> MemoryStore:
    store = runtime.get_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "memory_disabled",
                "message": "记忆系统未启用（MEMORY_ENABLED=off 或初始化失败）",
            },
        )
    return store


def _repo() -> MemoryRepository:
    return _store().memories


def _memory_item(memory: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": memory.get("id"),
        "scope": memory.get("scope"),
        "topic_id": memory.get("topic_id"),
        "kind": memory.get("kind"),
        "subject": memory.get("subject"),
        "content": memory.get("content"),
        "status": memory.get("status"),
        "confidence": memory.get("confidence"),
        "source_type": memory.get("source_type"),
        "sensitivity": memory.get("sensitivity"),
        "importance": memory.get("importance"),
        "pinned": memory.get("pinned"),
        "user_edited": memory.get("user_edited"),
        "valid_from": memory.get("valid_from"),
        "valid_to": memory.get("valid_to"),
        "last_confirmed_at": memory.get("last_confirmed_at"),
        "superseded_by": memory.get("superseded_by"),
        "version": memory.get("version"),
        "created_at": memory.get("created_at"),
        "updated_at": memory.get("updated_at"),
        "created_ts": iso_to_ms(memory.get("created_at")),
        "updated_ts": iso_to_ms(memory.get("updated_at")),
        "meta": memory.get("meta") if isinstance(memory.get("meta"), dict) else {},
    }


@router.get("/status")
def memory_status() -> Dict[str, Any]:
    cfg = get_settings()
    result = runtime.status()
    try:
        from app.memory.worker import get_worker

        worker_running = bool(get_worker().running())
    except Exception:  # noqa: BLE001 - 状态查询失败不能影响管理页
        worker_running = False
    result.update(
        {
            "context_enabled": bool(cfg.memory_context_enabled),
            "fast_memory": bool(cfg.memory_fast_memory),
            "upstream_history": str(cfg.memory_upstream_history),
            "upstream_memory": str(cfg.memory_upstream_memory),
            "recall_enabled": bool(cfg.memory_recall_enabled),
            "recall_implemented": True,
            "chat_topic_filter": bool(cfg.chat_topic_filter),
            "auto_extract": bool(cfg.memory_auto_extract),
            # `worker` 返回实际线程状态；配置值单独给出，避免“配置开、线程没起”误导。
            "worker": worker_running,
            "worker_running": worker_running,
            "worker_configured": bool(cfg.memory_worker),
            "extract_backend": str(cfg.memory_extract_backend),
            "cloud_extract": bool(cfg.memory_cloud_extract),
            "extract_debounce_seconds": int(cfg.memory_extract_debounce_seconds),
        }
    )
    return result


@router.get("/items")
def list_memories(
    status: Optional[str] = Query(None, max_length=16),
    kind: Optional[str] = Query(None, max_length=32),
    scope: Optional[str] = Query(None, max_length=16),
    topic_id: Optional[str] = Query(None, max_length=128),
    q: str = Query("", max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_deleted: bool = Query(False),
) -> Dict[str, Any]:
    if runtime.get_store() is None:
        return {"enabled": False, "items": [], "status": runtime.status()}
    repo = _repo()
    try:
        items = repo.list(
            status=status,
            kind=kind,
            scope=scope,
            topic_id=topic_id,
            q=q,
            limit=limit,
            offset=offset,
            include_deleted=include_deleted,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆列表读取失败：%s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_unavailable", "message": "记忆库暂时不可用"},
        ) from exc
    return {
        "enabled": True,
        "items": [_memory_item(memory) for memory in items],
        "status": runtime.status(),
    }


@router.post("/items", dependencies=[Depends(require_local_access)])
def create_memory(payload: MemoryCreate) -> Dict[str, Any]:
    repo = _repo()
    source = None
    if payload.source_quote or payload.source_message_id:
        source = {
            "message_id": payload.source_message_id or None,
            "session_id": payload.source_session_id or None,
            "quote": payload.source_quote,
        }
    try:
        memory = repo.create(
            content=payload.content,
            subject=payload.subject,
            kind=payload.kind,
            scope=payload.scope,
            topic_id=payload.topic_id or None,
            source_type=payload.source_type,
            sensitivity=payload.sensitivity,
            confidence=payload.confidence,
            importance=payload.importance,
            status=payload.status,
            pinned=payload.pinned,
            meta=payload.meta,
            source=source,
            actor="user",
            user_edited=True,
        )
    except MemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error": "conflict", "message": str(exc)}
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": str(exc)}
        )
    return {"enabled": True, "memory": _memory_item(memory)}


@router.get("/items/{memory_id}")
def get_memory(
    memory_id: str, include_deleted: bool = Query(False)
) -> Dict[str, Any]:
    repo = _repo()
    memory = repo.get(memory_id, include_deleted=include_deleted)
    if memory is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    return {"enabled": True, "memory": _memory_item(memory)}


@router.patch("/items/{memory_id}", dependencies=[Depends(require_local_access)])
def update_memory(memory_id: str, payload: MemoryUpdate) -> Dict[str, Any]:
    repo = _repo()
    try:
        memory = repo.update(
            memory_id,
            content=payload.content,
            subject=payload.subject,
            kind=payload.kind,
            scope=payload.scope,
            topic_id=payload.topic_id,
            importance=payload.importance,
            pinned=payload.pinned,
            sensitivity=payload.sensitivity,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            meta=payload.meta,
            actor="user",
            reason="用户编辑",
        )
    except MemoryNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    except MemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error": "conflict", "message": str(exc)}
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": str(exc)}
        )
    return {"enabled": True, "memory": _memory_item(memory)}


@router.delete("/items/{memory_id}", dependencies=[Depends(require_local_access)])
def delete_memory(
    memory_id: str,
    purge: bool = Query(False, description="true = 彻底删除（不可恢复）"),
) -> Dict[str, Any]:
    repo = _repo()
    ok = repo.delete(memory_id, purge=purge, actor="user")
    if not ok:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    return {"enabled": True, "ok": True, "purged": bool(purge)}


@router.post("/items/{memory_id}/confirm", dependencies=[Depends(require_local_access)])
def confirm_memory(memory_id: str) -> Dict[str, Any]:
    repo = _repo()
    try:
        memory = repo.confirm(memory_id, actor="user")
    except MemoryNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    except (ValueError, MemoryConflict) as exc:
        raise HTTPException(
            status_code=409, detail={"error": "conflict", "message": str(exc)}
        )
    return {"enabled": True, "memory": _memory_item(memory)}


@router.post("/items/{memory_id}/reject", dependencies=[Depends(require_local_access)])
def reject_memory(memory_id: str) -> Dict[str, Any]:
    repo = _repo()
    try:
        repo.reject(memory_id, actor="user", reason="用户拒绝")
    except MemoryNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    return {"enabled": True, "ok": True}


@router.post("/items/{memory_id}/restore", dependencies=[Depends(require_local_access)])
def restore_memory(memory_id: str) -> Dict[str, Any]:
    repo = _repo()
    try:
        memory = repo.restore(memory_id, actor="user")
    except MemoryNotFound:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "记忆不存在"}
        )
    except MemoryConflict as exc:
        raise HTTPException(
            status_code=409, detail={"error": "conflict", "message": str(exc)}
        )
    return {"enabled": True, "memory": _memory_item(memory)}


@router.get("/items/{memory_id}/sources")
def memory_sources(memory_id: str) -> Dict[str, Any]:
    repo = _repo()
    return {"enabled": True, "items": repo.sources(memory_id)}


@router.get("/items/{memory_id}/events")
def memory_events(
    memory_id: str, limit: int = Query(100, ge=1, le=500)
) -> Dict[str, Any]:
    repo = _repo()
    return {"enabled": True, "items": repo.events(memory_id, limit=limit)}


@router.get("/events")
def all_events(limit: int = Query(100, ge=1, le=500)) -> Dict[str, Any]:
    repo = _repo()
    return {"enabled": True, "items": repo.events(None, limit=limit)}


# ============================================================
# M3：任务队列
# ============================================================

@router.get("/export", dependencies=[Depends(require_local_access)])
def export_memories() -> Dict[str, Any]:
    store = _store()
    return store.export_memories()


@router.post("/import", dependencies=[Depends(require_local_access)])
def import_memories(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    store = _store()
    try:
        result = store.import_memories(data)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": str(exc)}
        ) from exc
    return {"enabled": True, **result}


@router.post("/clear", dependencies=[Depends(require_local_access)])
def clear_all(data: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    if str(data.get("confirm") or "") != "CLEAR ALL":
        raise HTTPException(
            status_code=400,
            detail={"error": "confirm_required", "message": "请输入 CLEAR ALL 确认"},
        )
    store = _store()
    counts = store.clear_all(purge_backups=bool(data.get("purge_backups")))
    return {"enabled": True, "cleared": counts}


@router.get("/jobs")
def list_jobs(
    status: Optional[str] = Query(None, max_length=16),
    kind: Optional[str] = Query(None, max_length=64),
    session_id: Optional[str] = Query(None, max_length=128),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    if runtime.get_store() is None:
        return {"enabled": False, "items": [], "counts": {}}
    store = _store()
    return {
        "enabled": True,
        "items": store.jobs.list_jobs(
            status=status,
            kind=kind,
            session_id=session_id,
            limit=limit,
            offset=offset,
        ),
        "counts": store.jobs.counts(),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    store = _store()
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "任务不存在"}
        )
    return {"enabled": True, "job": job}


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(require_local_access)])
def retry_job(job_id: str) -> Dict[str, Any]:
    store = _store()
    try:
        job = store.jobs.retry(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"error": "conflict", "message": str(exc)}
        )
    if job is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "message": "任务不存在"}
        )
    return {"enabled": True, "job": job}


__all__ = ["router"]
