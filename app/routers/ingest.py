"""入库状态与人工干预接口（Phase 8 / Phase 15）。

- `GET  /ingest/status/{file_id}`   查询单个文件的处理状态（前台轮询用）
- `POST /ingest/action/{file_id}`   人工动作：ocr / force / retry / ingest / skip
- `GET  /ingest/pending`            列出所有「等待确认入库」的作业（Phase 15 确认门）
- `POST /ingest/session`            「本次运行内全部允许」开关（Phase 15，内存态、不落盘）
- `GET  /ingest/content/{file_id}`  取回解析出的正文（Phase 15.2，改之前先看见）
- `POST /ingest/content/{file_id}`  保存改过的正文并重新质检（可顺带入库，Phase 15.2；
  Phase 17：图片资料用 `caption=true` + `doc_name` 写描述并给资料起名）

设计说明：
1. 这里只做 HTTP 适配，所有状态与决策都在 `app/services/ingest.py`；
2. 上游（LightRAG）进度为附加信息：拿不到也不影响主状态返回；
3. 动作冲突（正在处理 / 状态不允许 / 找不到记录）返回 409 / 404 与中文原因；
4. Phase 15：质检通过的文件会停在 state=review，等这里下发的 ingest / skip 决定去留；
5. Phase 15.1：同一动作重复提交（页面连点）不再重复执行，响应里用
   `duplicate_submit=true` 说明「这次点击已忽略」，前端据此提示并继续轮询真实状态；
6. Phase 15.2：正文编辑接口只改 `data/parsed/<file_id>/content.md`，
   改完**必须重新过质量门**（编辑不能变成绕过质检的后门），
   并且只有质检通过 + 用户点「保存并入库」时才会提交知识库。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.clients import lightrag
from app.core.config import get_settings
from app.models.schemas import IngestAction, IngestContent, IngestSession
from app.services import ingest

router = APIRouter()

logger = logging.getLogger(__name__)

DONE_STATES = ("processed", "completed", "success")
FAILED_STATES = ("failed", "error")


#: 进度查询的超时：进度只是附加信息，宁可这一轮不显示，也不能让状态接口卡 3 分钟。
#: 前端每 2.5 秒轮询一次；4 秒内拿不到就当作"这一轮没有进度"，下一轮再试。
PROGRESS_TIMEOUT = (2.0, 4.0)


def _progress_of(track_id: str) -> Dict[str, Any]:
    """查一次知识库侧的解析进度；失败时返回空字典（不抛异常）。"""
    if not track_id:
        return {}
    try:
        payload = lightrag.track_status(track_id, timeout=PROGRESS_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - 进度是附加信息，失败不影响主流程
        logger.debug("查询知识库进度失败（忽略）：%s", exc)
        return {}

    documents = payload.get("documents") or []
    states = [str(item.get("status") or "").lower() for item in documents]
    total = int(payload.get("total_count") or len(documents) or 0)
    done = sum(1 for state in states if state in DONE_STATES)
    failed = sum(1 for state in states if state in FAILED_STATES)

    if total <= 0:
        state = "unknown"
    elif done >= total:
        state = "done"
    elif failed and failed + done >= total:
        state = "failed"
    else:
        state = "processing"

    return {
        "state": state,
        "total": total,
        "processed": done,
        "failed": failed,
        "processing": max(total - done - failed, 0),
    }


@router.get("/ingest/status/{file_id}")
def ingest_status(file_id: str) -> Dict[str, Any]:
    payload = ingest.status(file_id)
    if not payload:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "ingest_not_found",
                "message": "找不到这个文件的处理记录（可能不是本次运行上传的）",
            },
        )
    return _with_progress(payload)


def _with_progress(payload: Dict[str, Any]) -> Dict[str, Any]:
    """补上知识库侧的解析进度（附加信息：拿不到就不写这个键，不影响主状态）。"""
    track_id = str((payload.get("lightrag") or {}).get("track_id") or "")
    if track_id:
        payload["progress"] = _progress_of(track_id)
    return payload


@router.get("/ingest/content/{file_id}")
def ingest_content(file_id: str) -> Dict[str, Any]:
    """取回解析出的正文（Phase 15.2「先改后入库」）。

    返回 `{content, chars, editable, reason, edit, max_chars}`：
    `editable=false` 时 `reason` 就是「为什么现在改不了」的人话
    （正在处理 / 已经入库 / 还没有正文）。
    """
    payload = ingest.content_for_edit(file_id)
    if not payload:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "ingest_not_found",
                "message": "找不到这个文件的处理记录（可能不是本次运行上传的）",
            },
        )
    return payload


@router.post("/ingest/content/{file_id}")
def ingest_content_save(file_id: str, data: IngestContent) -> Dict[str, Any]:
    """保存用户改过的正文 → **重新质检** →（可选）直接入库。

    body `{"content": "…", "ingest": false}`：
    · `ingest=false`（默认，按钮「保存」）：只保存 + 重新质检，仍停在确认门等你决定；
    · `ingest=true`（按钮「保存并入库」）：质检通过就等价于点一次「入库」。

    冲突一律 4xx + 中文原因：正文为空 400、太长 413、找不到 404、
    正在处理 / 已入库 / 没有正文 409。响应里的 `status` 是最新的完整状态
    （与 `GET /ingest/status/{file_id}` 同形），前端可以直接拿去重画卡片。
    """
    outcome = ingest.edit_content(
        file_id,
        data.content or "",
        ingest_now=bool(data.ingest),
        # Phase 17：图片资料的「写描述」（描述追加进正文 + 给这份资料起个名字）
        caption=bool(data.caption),
        doc_name=data.doc_name or "",
    )
    if not outcome.get("ok"):
        status = int(outcome.get("http_status") or 400)
        error = {
            400: "invalid_content",
            404: "ingest_not_found",
            409: "edit_conflict",
            413: "content_too_large",
        }.get(status, "edit_failed")
        raise HTTPException(
            status_code=status,
            detail={"error": error, "message": outcome.get("message") or "保存失败"},
        )

    payload = outcome.get("status")
    return {
        "success": True,
        "action": "edit",
        "state": outcome.get("state"),
        "message": outcome.get("message") or "已保存",
        "chars": outcome.get("chars"),
        "edited": bool(outcome.get("edited")),
        "ingested": bool(outcome.get("ingested")),
        "status": _with_progress(payload) if isinstance(payload, dict) else None,
    }


@router.get("/ingest/pending")
def ingest_pending() -> Dict[str, Any]:
    """列出等待确认入库的作业 + 当前确认门设置（Phase 15，网页刷新后据此恢复卡片）。"""
    settings = get_settings()
    return {
        "pending": ingest.pending_reviews(),
        # 页面刷新时只恢复“服务端确实还在忙”的作业；不把排队或提交失败伪装成进行中。
        "active": ingest.active_jobs(),
        "confirm": {
            "mode": ingest.confirm_mode(),
            "session_allow_all": ingest.session_allow_all(),
            "ttl_hours": settings.pending_review_ttl_hours,
        },
    }


@router.post("/ingest/session")
def ingest_session(data: IngestSession) -> Dict[str, Any]:
    """「本次运行内全部允许」开关（Phase 15）：打开后不再逐个停下确认。"""
    outcome = ingest.set_session_allow_all(bool(data.allow_all))
    return {"success": True, **outcome}


@router.post("/ingest/action/{file_id}")
def ingest_action(file_id: str, data: IngestAction) -> Dict[str, Any]:
    action = (data.action or "").strip().lower()
    outcome = ingest.activate(file_id, action)

    if not outcome.get("ok"):
        status = int(outcome.get("http_status") or 400)
        error = {
            400: "invalid_action",
            404: "ingest_not_found",
            409: "action_conflict",
        }.get(status, "action_failed")
        raise HTTPException(
            status_code=status,
            detail={"error": error, "message": outcome.get("message") or "操作失败"},
        )

    return {
        "success": True,
        "action": action,
        "state": outcome.get("state"),
        "message": outcome.get("message") or "已开始处理",
        # Phase 15.1：True = 这次请求没有重复执行（同一个动作已在队列/处理中）
        "duplicate_submit": bool(outcome.get("duplicate_submit")),
    }
