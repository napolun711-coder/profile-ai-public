"""POST /upload：文档上传（Phase 2 迁移；Phase 3 增强健壮性）。

契约（保持不变）：
- 表单字段：file（单数）—— LightRAG 1.5.6 的契约
- 返回键：success / filename / message / lightrag

Phase 3 变化：
1. 同步 requests 调用改为 asyncio.to_thread，避免阻塞事件循环；
2. 上传前校验扩展名（不通过 → 400）与文件大小（超限 → 413）；
3. 响应新增 track_id（上游 InsertResponse 自带），仅在拿到时追加，不影响既有键。

Phase 8 变化（本地解析 + 质量门）：
1. 上传后不再直接转发原文件，而是交给 `services/ingest.py`：本地解析 → 质量检测 → 合格才入库；
2. 立即返回 `file_id`（已受理），前端用 `GET /ingest/status/{file_id}` 看进度；
3. 契约保留：表单字段仍是 file、返回键 success/filename/message/lightrag/saved_path 不变，
   另新增 file_id / state / detail；`lightrag` 由"上游原始响应"变为"入库阶段状态占位"。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.clients import lightrag
from app.core.config import get_settings
from app.core.paths import PROFILE_AI_HOME, UPLOAD_DIR
from app.services import ingest

router = APIRouter()

logger = logging.getLogger(__name__)

# 与改造前 main.py 的返回文案保持一致的键名与风格（Phase 8 起说明改为"受理并质检"）
UPLOAD_ACCEPTED_MESSAGE = "文件已受理，正在本地解析与质量检测"

FALLBACK_FILENAME = "upload.bin"

UPLOAD_ARCHIVE_ROOT = UPLOAD_DIR


def _safe_filename(name: str) -> str:

    base = Path(name or FALLBACK_FILENAME).name

    kept = [ch if (ch.isalnum() or ch in "._-") else "_" for ch in base]

    cleaned = "".join(kept).strip(" .")

    return cleaned or FALLBACK_FILENAME


def _archive_upload(filename: str, content: bytes) -> str:

    """把上传原件留档到 data/uploads/YYYYMMDD/；失败不影响上传主流程。"""

    try:

        now = datetime.now()

        target_dir = UPLOAD_ARCHIVE_ROOT / now.strftime("%Y%m%d")

        target_dir.mkdir(parents=True, exist_ok=True)

        target = target_dir / (now.strftime("%H%M%S") + "_" + _safe_filename(filename))

        index = 1

        while target.exists():

            target = target_dir / (now.strftime("%H%M%S") + "_" + str(index) + "_" + _safe_filename(filename))

            index += 1

        target.write_bytes(content)

        logger.info("上传原件已留档：%s", target)

        return str(target.relative_to(PROFILE_AI_HOME)).replace(chr(92), "/")

    except Exception as exc:

        logger.warning("上传原件留档失败（不影响上传）：%s", exc)

        return ""


def _check_extension(filename: str) -> None:

    allowed = get_settings().allowed_extensions

    if not allowed:
        return

    lowered = (filename or "").lower()

    if not any(lowered.endswith(ext.lower()) for ext in allowed):

        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_file_type",
                "message": f"不支持的文件类型：{filename or FALLBACK_FILENAME}",
                "allowed_extensions": list(allowed),
            },
        )


def _check_size(size: int) -> None:

    limit = get_settings().max_upload_size

    if limit and size > limit:

        raise HTTPException(
            status_code=413,
            detail={
                "error": "file_too_large",
                "message": f"文件超过上限（{limit} 字节）",
                "max_upload_size": limit,
                "size": size,
            },
        )


@router.post("/upload")
async def upload_file(

    file: UploadFile = File(...)

):

    filename = file.filename or FALLBACK_FILENAME

    declared_size = getattr(file, "size", None)

    _check_extension(filename)

    if declared_size:
        _check_size(declared_size)

    content = await file.read()

    _check_size(len(content))

    archived = _archive_upload(filename, content)
    # ==========================
    # Phase 8：改为「本地解析 + 质量门」闸门
    #   只有通过质检的文本才会由 services/ingest.py 提交给 LightRAG；
    #   返回键 success/filename/message/lightrag/saved_path 全部保留，
    #   success 表示"已受理"，最终结论用 file_id 查 /ingest/status/{file_id}。
    # ==========================

    job = ingest.create_job(

        filename=filename,

        content=content,

        content_type=file.content_type or "",

        archived_path=archived

    )

    if get_settings().ingest_async:

        ingest.enqueue(job["file_id"])

        final = job

    else:

        # 同步模式（INGEST_ASYNC=false）：阻塞到处理结束，便于排障
        final = await asyncio.to_thread(ingest.run_job, job["file_id"]) or job

    lightrag_state = final.get("lightrag") or {}

    return {

        "success": True,

        "filename": file.filename,

        "message": UPLOAD_ACCEPTED_MESSAGE,

        "file_id": job["file_id"],

        "state": final.get("state"),

        "detail": final.get("detail") or "",

        "saved_path": archived,

        "lightrag": {

            "state": lightrag_state.get("state") or "queued",

            "track_id": lightrag_state.get("track_id") or ""

        }

    }

DONE_STATES = ("processed", "completed", "success")
FAILED_STATES = ("failed", "error")


def _normalize_state(states, total: int) -> str:
    # 把 LightRAG 的文档状态归一化为 processing / done / failed / unknown
    if total <= 0:
        return "unknown"
    done = sum(1 for item in states if item in DONE_STATES)
    failed = sum(1 for item in states if item in FAILED_STATES)
    if failed and failed + done >= total:
        return "failed"
    if done >= total:
        return "done"
    return "processing"


@router.get("/documents/status/{track_id}")
def documents_status(track_id: str) -> Dict[str, Any]:
    # 给网页用的解析进度：把上游 track_status 归一化成 {state, total, processed, failed}
    payload = lightrag.track_status(track_id)
    documents = payload.get("documents") or []
    states = [str(item.get("status") or "").lower() for item in documents]
    total = int(payload.get("total_count") or len(documents) or 0)
    done = sum(1 for item in states if item in DONE_STATES)
    failed = sum(1 for item in states if item in FAILED_STATES)
    return {
        "track_id": track_id,
        "state": _normalize_state(states, total),
        "total": total,
        "processed": done,
        "failed": failed,
        "processing": max(total - done - failed, 0),
        "status_summary": payload.get("status_summary") or {},
        "documents": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "chunks_count": item.get("chunks_count"),
                "content_summary": (item.get("content_summary") or "")[:60],
            }
            for item in documents
        ],
    }

