"""文档管理接口（Phase 16）：列出 / 打开原件 / 打开文件夹 / 删除。

分工：本模块只做 HTTP 适配与「权限 + 人话」，
    · 上游数据来自 `clients/lightrag.py`（Phase 16 新增的文档管理三件套 + 删除）；
    · 本机档案与原件定位来自 `services/library.py`、`services/origin.py`；
    · 入库、留档、跳过等一切写操作仍然只在 `services/ingest.py`。

接口一览：
    GET  /api/files/list          知识库文档 + 本机档案（含原件可用性），支持筛选/分页/搜索
    GET  /api/files/overview      顶部统计：状态计数、管线忙不忙、各目录规模、轮询状态
    GET  /api/files/resolve       某个文档对应哪份原件（前端用它给参考资料打标记）
    GET  /api/files/raw           打开原件（浏览器新标签；找不到时返回一页人话说明）
    GET  /api/images/asset        P2G：图片引用的缩略图（作业目录内的图片；只读）
    POST /api/files/open-folder   在资源管理器里打开某个数据目录（仅本机 + 令牌）
    POST /api/files/delete        删除（三档范围 + 原件开关；仅本机 + 令牌 + 同源）

权限（沿用 Phase 10 的两档校验，不另造一套）：
    · 只读类（list / overview / resolve / raw）走 `guard_remote` —— 手机 / 局域网也能看、
      也能把原件下载到手机上（停不掉任何东西，删不了任何东西）；
    · 会动数据的（open-folder / delete）走 `guard` —— 仅本机 127.0.0.1 + 每次启动的随机令牌
      + 同源 Origin；手机页面连按钮都不显示，硬调也是 403。

删除的范围（三档，页面上要逐条写清楚）：
    kb    = 只从知识库移除文档记录（本机留档与原件都留着）；
    local = kb + 删掉本机留档（knowledge\\ 里的 md 与 data\\parsed\\<file_id>\\）；
    all   = local + 让上游把它 input 目录里那份我们上传的 .md 拷也删掉
            （`delete_file=true`，唯一会碰到 LightRAG 安装目录的边界，默认不选）。
    原件（data\\originals、data\\uploads 里的原始文件）默认**不动**，要删必须额外勾选
    `remove_originals=true` —— 原件是最贵的东西，不能跟着「清理记录」一起没了。
"""

from __future__ import annotations

import html as html_module
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.clients import lightrag
from app.core.chat_access import require_local_access
from app.core.errors import UpstreamError, humanize_upstream
from app.core.paths import (
    DATA_DIR,
    INBOX_DIR,
    KNOWLEDGE_DIR,
    ORIGINALS_DIR,
    OUTPUT_DIR,
    PARSED_DIR,
    STATIC_DIR,
    UPLOAD_DIR,
)
from app.models.schemas import DocumentTopicUpdate, FilesDelete, FolderOpen
from app.memory import runtime
from app.memory.store import document_key
from app.routers import control
from app.services import gallery, image_evidence, ingest, library, origin

router = APIRouter(tags=["files"])

logger = logging.getLogger(__name__)

#: 搜索时最多翻几页上游（每页 200）——个人资料库够用，且不会把上游拖慢
SEARCH_MAX_PAGES = 5
SEARCH_PAGE_SIZE = 200

#: `/api/files/list` 单次返回的本机档案行上限。
#: 上限本身不是问题（每行都要 `origin.resolve_for()` 去磁盘查原件在不在，
#: 档案攒到成千上万条时接口会变慢），**"超了不说"才是** ——
#: 所以超限时响应里会一起回 `local_only_total`，页面据此明说「另有 N 条未载入」。
LOCAL_ONLY_LIMIT = 500

#: 判断「上游因为管线忙而拒绝删除」的关键词（英文原文，见 _delete_message）
BUSY_MARKERS = ("busy", "pipeline", "in progress", "processing", "locked", "running")

#: 可以「在资源管理器里打开」的目录（名称 → 路径；`mkdir=True` 表示不存在就建出来）
FOLDER_TARGETS: Dict[str, Dict[str, Any]] = {
    "inbox": {"label": "收件箱（丢进来即自动入库）", "path": INBOX_DIR, "mkdir": True},
    "originals": {"label": "原件留档（轮询入库的原件）", "path": ORIGINALS_DIR, "mkdir": True},
    "uploads": {"label": "上传留档（网页上传的原件）", "path": UPLOAD_DIR, "mkdir": True},
    "knowledge": {"label": "知识库留档（解析后的正文）", "path": KNOWLEDGE_DIR, "mkdir": False},
    "parsed": {"label": "解析工作目录", "path": PARSED_DIR, "mkdir": False},
    "output": {"label": "产出目录", "path": OUTPUT_DIR, "mkdir": True},
    "data": {"label": "数据目录", "path": DATA_DIR, "mkdir": True},
}


def inbox_module():
    """延迟导入 `services/inbox.py`。

    为什么延迟：这个模块只用到它的「状态查询 / 立即扫描」两个入口，
    而它是带后台线程的模块 —— 放在函数里导入，可以在不启动线程的场景
    （单测、只想看看列表）下也不会牵连到它。
    """
    from app.services import inbox as module

    return module


# ============================================================
# 页面路由（/files）
# ============================================================

@router.get("/files")
def files_page() -> FileResponse:
    """可视化文档管理页（与 /web 的对话页、/settings 的设置页并列）。"""
    return FileResponse(
        STATIC_DIR / "files.html",
        headers={"Cache-Control": "no-cache"},
    )


# ============================================================
# 组装「一条文档」
# ============================================================

def _topic_maps() -> tuple:
    """文档键 → topic_id，以及 topic_id → 名称；记忆库不可用时返回空表。"""
    try:
        store = runtime.get_store()
    except Exception:  # noqa: BLE001
        store = None
    if store is None:
        return {}, {}
    try:
        topic_map = store.document_topic_map()
        names = {
            str(item.get("id") or ""): str(item.get("name") or "")
            for item in store.list_topics(status="")
        }
    except Exception:  # noqa: BLE001 - 旧库/迁移异常时按无标签显示
        return {}, {}
    return topic_map, names


def _row(
    doc: Dict[str, Any],
    index: Dict[str, List[Dict[str, Any]]],
    topic_map: Optional[Dict[str, str]] = None,
    topic_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """上游文档 + 本机档案 + 原件信息 = 页面要的一行。"""
    file_path = str(doc.get("file_path") or "")
    status = str(doc.get("status") or "")
    record = library.match_document(file_path, index=index)
    info = origin.resolve_for(file_path=file_path, record=record, index=index)

    name = str((record or {}).get("name") or "") or file_path or str(doc.get("id") or "")
    topic_id = (topic_map or {}).get(document_key(file_path), "")
    return {
        "doc_id": str(doc.get("id") or ""),
        "file_path": file_path,
        "name": name,
        "topic_id": topic_id,
        "topic_name": (topic_names or {}).get(topic_id, ""),
        "status": status,
        "status_label": library.upstream_status_label(status),
        "chunks_count": doc.get("chunks_count"),
        "content_length": int(doc.get("content_length") or 0),
        "created_at": str(doc.get("created_at") or ""),
        "updated_at": str(doc.get("updated_at") or ""),
        "track_id": str(doc.get("track_id") or ""),
        "error_msg": str(doc.get("error_msg") or ""),
        # ---- 本机侧 ----
        "has_local": bool(record),
        "file_id": str((record or {}).get("file_id") or ""),
        "local_state": str((record or {}).get("state") or ""),
        "local_state_label": str((record or {}).get("state_label") or ""),
        "local_origin": str((record or {}).get("origin") or ""),
        "local_detail": str((record or {}).get("detail") or ""),
        "content_chars": int((record or {}).get("content_chars") or 0),
        "quality_level": str((record or {}).get("quality_level") or ""),
        "size": int((record or {}).get("size") or 0),
        "knowledge_path": str((record or {}).get("knowledge_path") or ""),
        "archived_path": str((record or {}).get("archived_path") or ""),
        # ---- 原件 ----
        "origin": info,
        "can_open": bool(info.get("available")),
        # 删除按钮的可用性：知识库里有记录（doc_id）就能删记录；有本机档案就能删留档
        "can_delete": bool(doc.get("id") or (record or {}).get("file_id")),
    }


def _local_only_row(
    record: Dict[str, Any],
    topic_map: Optional[Dict[str, str]] = None,
    topic_names: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """本机有档案、知识库里查不到的行（等确认 / 已跳过 / 质检没过 / 入库失败）。"""
    file_path = str(record.get("markdown_name") or "")
    info = origin.resolve_for(file_id=str(record.get("file_id") or ""), record=record)
    topic_id = (topic_map or {}).get(document_key(file_path), "")
    return {
        "doc_id": "",
        "file_path": file_path,
        "name": str(record.get("name") or record.get("file_id") or ""),
        "topic_id": topic_id,
        "topic_name": (topic_names or {}).get(topic_id, ""),
        "status": "",
        "status_label": str(record.get("state_label") or ""),
        "chunks_count": None,
        "content_length": int(record.get("content_chars") or 0),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "track_id": str(record.get("track_id") or ""),
        "error_msg": "",
        "has_local": True,
        "file_id": str(record.get("file_id") or ""),
        "local_state": str(record.get("state") or ""),
        "local_state_label": str(record.get("state_label") or ""),
        "local_origin": str(record.get("origin") or ""),
        "local_detail": str(record.get("detail") or ""),
        "content_chars": int(record.get("content_chars") or 0),
        "quality_level": str(record.get("quality_level") or ""),
        "size": int(record.get("size") or 0),
        "knowledge_path": str(record.get("knowledge_path") or ""),
        "archived_path": str(record.get("archived_path") or ""),
        "origin": info,
        "can_open": bool(info.get("available")),
        "can_delete": True,
    }


def _search_upstream(query: str, status: str) -> tuple:
    """带搜索词时多翻几页再在本地筛（上游的 /documents/paginated 不支持按名搜索）。

    返回 `(docs, pagination, counts)`；`pagination` 是本地的（筛完了才知道有多少条），
    并额外带上 `scanned`：页面据此说明「在最近扫过的 N 篇里找的」。
    """
    wanted = (query or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    counts: Dict[str, Any] = {}
    scanned = 0

    for current in range(1, SEARCH_MAX_PAGES + 1):
        payload = lightrag.documents_paginated(
            page=current, page_size=SEARCH_PAGE_SIZE, status=status
        )
        if current == 1:
            counts = dict(payload.get("status_counts") or {})
        documents = list(payload.get("documents") or [])
        scanned += len(documents)
        for doc in documents:
            haystack = " ".join(
                [
                    str(doc.get("file_path") or ""),
                    str(doc.get("id") or ""),
                    str(doc.get("content_summary") or ""),
                ]
            ).lower()
            if wanted in haystack:
                hits.append(doc)
        if not (payload.get("pagination") or {}).get("has_next"):
            break

    pagination = {
        "page": 1,
        "page_size": SEARCH_PAGE_SIZE,
        "total_count": len(hits),
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
        "scanned": scanned,
    }
    return hits, pagination, counts


@router.get("/api/files/list")
def files_list(
    request: Request,
    status: str = "",
    page: int = 1,
    page_size: int = 50,
    q: str = "",
    topic_id: str = "",
    sort: str = "updated_at",
    direction: str = "desc",
) -> Dict[str, Any]:
    """列出知识库文档，并补上「本机档案 + 原件能不能打开 + 资料集归属」。

    只读接口（`guard_remote`）：手机 / 局域网也能看。知识库连不上时**不报错**，
    而是降级成「只显示本机处理过的文件」，原因写在 `upstream.message` 里 ——
    这个页面的首要用途是「我的资料在哪」，知识库挂了也得能看。
    """
    control.guard_remote(request)

    records = library.local_records()
    index = library.build_index(records)
    topic_map, topic_names = _topic_maps()
    upstream: Dict[str, Any] = {"ok": True, "message": ""}
    documents: List[Dict[str, Any]] = []
    pagination: Dict[str, Any] = {}
    counts: Dict[str, Any] = {}
    searched = bool(q.strip())
    topic_filtered = bool(str(topic_id or "").strip())

    try:
        if searched:
            documents, pagination, counts = _search_upstream(q, status)
        elif topic_filtered:
            # 资料集过滤要扫全量再本地筛，否则当前页可能正好没有该案文档。
            documents, pagination, counts = _search_upstream("", status)
        else:
            payload = lightrag.documents_paginated(
                page=page,
                page_size=page_size,
                status=status,
                sort_field=sort,
                sort_direction=direction,
            )
            documents = list(payload.get("documents") or [])
            pagination = dict(payload.get("pagination") or {})
            counts = dict(payload.get("status_counts") or {})
    except UpstreamError as exc:
        upstream = {"ok": False, "message": exc.message}
        documents = []
        logger.warning("文档列表降级为本机视图：%s", exc.message)

    rows = [_row(doc, index, topic_map, topic_names) for doc in documents]
    if searched:
        # 上游只按文档名/摘要筛；本机原名也可能命中搜索词，这里再筛一遍
        wanted = q.strip().lower()
        rows = [row for row in rows if wanted in (str(row["name"]) + " " + str(row["file_path"])).lower()]
    if topic_filtered:
        rows = [row for row in rows if str(row.get("topic_id") or "") == topic_id]

    # 「本机有档案、但没进知识库」的行（等确认 / 已跳过 / 质检没过 / 入库失败 / 已删除）
    local_only = [
        _local_only_row(record, topic_map, topic_names)
        for record in records
        if str(record.get("state") or "") != ingest.STATE_DONE
    ]
    if searched:
        wanted = q.strip().lower()
        local_only = [row for row in local_only if wanted in str(row["name"]).lower()]
    if topic_filtered:
        local_only = [
            row for row in local_only if str(row.get("topic_id") or "") == topic_id
        ]
    # 截断之前先记下真实条数：以前这里是裸的 `local_only[:50]`，
    # 实测真实 63 条时另外 13 条**在任何页都看不到、页面也不提**。
    # 前端本来就是「一次拿全部、前端切片」分页（见 files.html 的 state.localPage），
    # 所以这里放宽上限就够了，不必改成服务端分页。
    local_only_total = len(local_only)
    local_only = local_only[:LOCAL_ONLY_LIMIT]

    notice = ""
    if not upstream["ok"]:
        notice = "知识库暂时不可用：下面只显示本机处理过的文件（等它恢复后刷新即可）。"
    elif searched:
        notice = (
            f"已在最近 {pagination.get('scanned', 0)} 篇里筛出 "
            f"{len(rows)} 篇（每页最多扫 {SEARCH_MAX_PAGES * SEARCH_PAGE_SIZE} 篇）。"
        )
    elif topic_filtered:
        notice = f"已按资料集筛选出 {len(rows)} 篇；本机未入库的同类文档也会一并显示。"

    return {
        "success": True,
        "upstream": upstream,
        "documents": rows,
        "pagination": pagination,
        "counts": counts,
        "topic_id": topic_id,
        "local_only": local_only,
        # 真实条数 vs 本次返回条数：不相等时页面必须说出来（见 LOCAL_ONLY_LIMIT）
        "local_only_total": local_only_total,
        "local_only_limit": LOCAL_ONLY_LIMIT,
        "folders": _folder_summary(),
        "notice": notice,
        "searched": searched,
    }


@router.get("/api/files/topic")
def files_get_topic(
    request: Request,
    file_path: str = Query(..., min_length=1, max_length=500),
) -> Dict[str, Any]:
    """查单个文档的资料集标签（只读）。"""
    control.guard_remote(request)
    store = runtime.get_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_disabled", "message": "资料集模型未启用（MEMORY_ENABLED=off）"},
        )
    return {"success": True, "item": store.document_topic(file_path)}


@router.post(
    "/api/files/topic",
    dependencies=[Depends(require_local_access)],
)
def files_set_topic(data: DocumentTopicUpdate) -> Dict[str, Any]:
    """给文档打资料集标签；仅本机可写。"""
    store = runtime.get_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_disabled", "message": "资料集模型未启用（MEMORY_ENABLED=off）"},
        )
    try:
        item = store.set_document_topic(
            data.file_path,
            data.topic_id,
            name=data.name,
            file_id=data.file_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid", "message": str(exc)}
        ) from exc
    return {"success": True, "item": item}


@router.delete(
    "/api/files/topic",
    dependencies=[Depends(require_local_access)],
)
def files_clear_topic(
    file_path: str = Query(..., min_length=1, max_length=500),
) -> Dict[str, Any]:
    """解除文档的资料集标签；仅本机可写。"""
    store = runtime.get_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "memory_disabled", "message": "资料集模型未启用（MEMORY_ENABLED=off）"},
        )
    return {"success": True, "cleared": store.clear_document_topic(file_path)}


def _folder_summary() -> List[Dict[str, Any]]:
    """各目录的规模（条数 / 字节）——用于管理页的「文件夹」卡片。

    只数**文件**、不递归太深（`data\\originals\\YYYY-MM\\` 这类一层子目录正好），
    并且限制扫描条数：目录异常大时页面也不会卡住（宁可少算，不拖慢接口）。
    """
    result: List[Dict[str, Any]] = []
    for key, item in FOLDER_TARGETS.items():
        path: Path = item["path"]
        files = 0
        total = 0
        truncated = False
        if path.is_dir():
            entries = list(path.rglob("*"))
            truncated = len(entries) > 2000
            for candidate in entries[:2000]:
                if candidate.is_file():
                    files += 1
                    try:
                        total += candidate.stat().st_size
                    except OSError:      # 扫描期间文件被移走：少算一个不影响结论
                        continue
        result.append({
            "key": key,
            "label": item["label"],
            "relative": origin.relative_path(path),
            "path": str(path),
            "exists": path.is_dir(),
            "files": files,
            "bytes": total,
            "truncated": truncated,
        })
    return result


# ============================================================
# 概览（顶部统计 / 管线状态 / 目录规模 / 轮询状态）
# ============================================================

@router.get("/api/files/overview")
def files_overview(request: Request) -> Dict[str, Any]:
    """管理页顶部要的一切：状态计数、管线忙不忙、各目录规模、轮询状态、能做什么。"""
    control.guard_remote(request)

    upstream: Dict[str, Any] = {"ok": True, "message": "", "url": lightrag.base_url()}
    counts: Dict[str, Any] = {}
    pipeline: Dict[str, Any] = {}
    try:
        counts = dict(lightrag.status_counts().get("status_counts") or {})
    except UpstreamError as exc:
        upstream = {"ok": False, "message": exc.message, "url": lightrag.base_url()}
    if upstream["ok"]:
        try:
            raw = lightrag.pipeline_status()
            pipeline = {
                "busy": bool(raw.get("busy")),
                "docs": int(raw.get("docs") or 0),
                "job_name": str(raw.get("job_name") or ""),
                "latest_message": str(raw.get("latest_message") or ""),
                "recovery_required": bool(raw.get("recovery_required")),
                "recovery_message": str(raw.get("recovery_message") or ""),
            }
        except UpstreamError as exc:      # 计数拿到了、管线状态没拿到：不影响主信息
            pipeline = {"busy": None, "error": exc.message}

    records = library.local_records()
    local = {
        "jobs": len(records),
        "in_knowledge_base": sum(1 for item in records if item.get("state") == ingest.STATE_DONE),
        "waiting_review": sum(1 for item in records if item.get("state") == ingest.STATE_REVIEW),
        "knowledge_files": len(library.knowledge_files()),
    }
    inbox = inbox_module().status()
    folders = _folder_summary()
    folder_index = {item["key"]: item for item in folders}

    return {
        "success": True,
        "upstream": upstream,
        "counts": counts,
        "pipeline": pipeline,
        "local": local,
        "inbox": inbox,
        # 手机 / 局域网页面：只能看和下载，不能删、也不能在电脑上打开文件夹
        "capabilities": {
            "local_page": control.client_is_local(request),
            "can_delete": control.client_is_local(request),
            "can_open_folder": bool(control.client_is_local(request) and os.name == "nt"),
        },
        "folders": folders,
        "folder_index": folder_index,
        "notice": "" if upstream["ok"] else "知识库暂时不可用（统计与列表都会降级），服务恢复后刷新即可。",
    }


# ============================================================
# 原件：定位 / 打开
# ============================================================

@router.get("/api/files/resolve")
def files_resolve(
    request: Request,
    file_id: str = "",
    file_path: str = "",
) -> Dict[str, Any]:
    """这条文档对应本机的哪份文件（前端用它给参考资料打「可双击 / 找不到」的标记）。"""
    control.guard_remote(request)
    if not file_id and not file_path:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_target", "message": "请给出 file_id 或 file_path"},
        )
    return {"success": True, **origin.resolve_for(file_id=file_id, file_path=file_path)}


def _file_response(path: Path) -> FileResponse:
    """把文件交给浏览器：inline 预览 + RFC 5987 文件名（中文名不会变乱码）。"""
    return FileResponse(
        path,
        media_type=origin.media_type_of(path),
        headers={
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(path.name, safe=""),
            "Cache-Control": "no-store",
        },
    )


def _missing_html(info: Dict[str, Any], *, title: str = "找不到这份资料的原件") -> HTMLResponse:
    """找不到文件时回**一页人话**（而不是一坨 JSON）。

    为什么用 HTML：这个地址是浏览器新标签页里的地址栏（用户双击参考资料打开），
    返回 JSON 只会让用户看到花括号。说明里必须包含「为什么」与「还能怎么办」。
    """
    name = html_module.escape(str(info.get("name") or info.get("file_path") or "（未知文件）"))
    reason = html_module.escape(str(info.get("reason") or "本机上找不到这个文件。"))
    return HTMLResponse(
        status_code=404,
        content=(
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{title}</title></head>"
            "<body style=\"font-family:system-ui,'Microsoft YaHei',sans-serif;"
            "max-width:640px;margin:12vh auto;padding:0 20px;color:#1f2937;line-height:1.8\">"
            f"<h1 style=\"font-size:20px\">{title}</h1>"
            f"<p style=\"font-size:15px\"><b>{name}</b></p>"
            f"<p style=\"font-size:14px;color:#4b5563\">{reason}</p>"
            "<p style=\"font-size:13px;color:#6b7280\">可以回到「文件管理」页（/files）看本机留下了哪些文件，"
            "或者把原件重新放进 <code>data\\inbox\\</code> 让它自动入库。</p>"
            "</body></html>"
        ),
    )


@router.get("/api/files/raw")
def files_raw(
    request: Request,
    file_id: str = "",
    file_path: str = "",
    path: str = "",
) -> Any:
    """打开原件（浏览器新标签）。

    · 优先按 `file_id`（本机作业号，最精确）/ `file_path`（知识库里的文档名）解析；
    · 也接受 `path`，但必须落在 `data\\`、`knowledge\\`、`output\\` 之内
      （`origin.safe_path` 会把 `..\\..` 与系统目录挡掉），所以这个参数不构成越权读盘；
    · 找不到时返回一页人话的 404（见 `_missing_html`）。
    """
    control.guard_remote(request)

    if path and not file_id and not file_path:
        target = origin.safe_path(path)
        if target is None:
            return _missing_html(
                {
                    "name": Path(str(path)).name,
                    "reason": "这个路径不在允许范围内：只能打开 data\\、knowledge\\、output\\ 下"
                              "**已存在**的文件（项目外的路径一律拒绝）。",
                }
            )
        return _file_response(target)

    info = origin.resolve_for(file_id=file_id, file_path=file_path)
    if not info.get("available"):
        return _missing_html(info)
    return _file_response(Path(str(info["path"])))


# ============================================================
# P2G：图片引用缩略图（只读，按 file_id + image_id 取作业目录内的图片）
# ============================================================

#: 作业号只允许字母数字与 `._-`：它会被拼进 `data/parsed/<file_id>/`，
#: 任何分隔符或 `..` 都不该走到文件系统。
FILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


@router.get("/api/images/asset")
def image_asset(
    request: Request,
    file_id: str = "",
    image_id: str = "",
) -> Any:
    """给 `/chat` 的 `[img-n]` 卡片提供缩略图；找不到时 404（前端显示占位图）。

    只读：只查 gallery 证据并读作业目录内的图片文件，不重建索引、不写盘、不联网。
    路径边界：`asset_paths` 虽是证据里来的，仍然重新做一次 `resolve()` + 相对目录校验，
    防止旧档案里的异常路径把任意文件读出来。
    """
    control.guard_remote(request)

    target = _image_asset_path(file_id=file_id, image_id=image_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "image_not_found", "message": "这张图片没有可预览的本机文件"},
        )
    return FileResponse(
        target,
        media_type=origin.media_type_of(target),
        headers={
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(target.name, safe=""),
            "Cache-Control": "private, max-age=300",
        },
    )


def _image_asset_path(*, file_id: str, image_id: str) -> Any:
    """返回该图片证据对应的本机图片文件；条件不满足一律 None。"""
    if not FILE_ID_RE.match(str(file_id or "")):
        return None
    if not str(image_id or "").strip():
        return None

    record = _image_record(file_id, image_id)
    if record is None:
        return None

    job_dir = _job_dir(str(record.get("file_id") or file_id))
    if job_dir is not None:
        for asset in record.get("asset_paths") or []:
            candidate = _inside_job(job_dir, asset)
            if candidate is not None:
                return candidate

    # 没有可用的内嵌资源时，退到原件——仅在原件本身就是图片时（PDF/DOCX 不能当 <img>）。
    info = origin.resolve_for(file_id=str(record.get("file_id") or file_id))
    if not info.get("available"):
        return None
    original = Path(str(info.get("path") or ""))
    if original.is_file() and origin.media_type_of(original).startswith("image/"):
        return original
    return None


def _image_record(file_id: str, image_id: str) -> Any:
    """按 image_id 找图片记录；只读（image_records 会按需派生 v1 条目的证据）。"""
    wanted = str(image_id or "").strip()
    for item in image_evidence.image_records():
        if not isinstance(item, dict):
            continue
        if str(item.get("file_id") or "") != file_id:
            continue
        if str(item.get("image_id") or "") != wanted:
            continue
        return item
    return None


def _job_dir(file_id: str) -> Any:
    if not FILE_ID_RE.match(file_id):
        return None
    try:
        root = (gallery.parsed_dir() / file_id).resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def _inside_job(job_dir: Path, asset: Any) -> Any:
    """资产相对路径必须在作业目录内、是实际存在的图片文件。"""
    text = str(asset or "").strip()
    if not text:
        return None
    try:
        candidate = (job_dir / text).resolve()
        candidate.relative_to(job_dir)
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    if not origin.media_type_of(candidate).startswith("image/"):
        return None
    return candidate


# ============================================================
# 打开文件夹（仅本机 + 令牌；容器 / 非 Windows 明确拒绝）
# ============================================================

def _no_window_flags() -> int:
    """`CREATE_NO_WINDOW` 的唯一来源是 scripts\\lifecycle.py（新增外部命令一律套用）。

    规则要求「不得出现控制台窗」：`explorer.exe` 虽然不会留窗口，但走同一条路径
    可以少一个例外，也避免以后有人在这里加打印命令时漏掉。
    """
    module = control.lifecycle_module()
    if module is not None:
        try:
            return int(module.no_window_flags())
        except Exception:                     # noqa: BLE001 - 拿不到就用 subprocess 的常量
            logger.debug("lifecycle.no_window_flags() 不可用，退回 subprocess 常量")
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


@router.post("/api/files/open-folder")
def files_open_folder(payload: FolderOpen, request: Request) -> Dict[str, Any]:
    """在资源管理器里打开某个数据目录（仅本机页面能用）。"""
    control.guard_action(request, payload.token)

    key = str(payload.target or "inbox").strip().lower()
    item = FOLDER_TARGETS.get(key)
    if item is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_folder",
                "message": "不认识的目录名：" + key,
                "allowed": list(FOLDER_TARGETS),
            },
        )

    path: Path = item["path"]
    if os.name != "nt":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_windows",
                "message": "容器模式下没法在宿主机打开资源管理器：请直接访问配置里挂载的数据目录"
                           "（见 docs\\Docker部署手册.md 的数据目录一节）。",
            },
        )
    try:
        if item.get("mkdir") and not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "folder_unavailable", "message": f"目录不可用（{path}）：{exc}"},
        ) from exc
    if not path.is_dir():
        raise HTTPException(
            status_code=404,
            detail={
                "error": "folder_missing",
                "message": f"目录还不存在：{origin.relative_path(path)}（还没有文件进过这里）",
            },
        )

    try:
        subprocess.Popen(["explorer", str(path)], creationflags=_no_window_flags())
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "open_failed", "message": f"打不开资源管理器：{exc}"},
        ) from exc

    relative = origin.relative_path(path)
    logger.info("在资源管理器里打开目录：%s", relative)
    return {
        "success": True,
        "opened": str(path),
        "relative": relative,
        "label": item["label"],
        "message": f"已在资源管理器里打开：{relative}",
    }


# ============================================================
# 删除（仅本机 + 令牌 + 同源；三档范围 + 原件开关）
# ============================================================

DELETE_SCOPES = {
    "kb": "只从知识库移除记录（本机留档与原件都留着）",
    "local": "知识库记录 + 本机留档（knowledge 与 data\\parsed）",
    "all": "知识库记录 + 本机留档 + 上游 input 里那份 md 拷贝",
}

#: 上游「还在建索引」的状态。真机实测（Phase 16 自测）：对一篇正在解析的文档发删除请求，
#: LightRAG 会回 200「deletion_started」，但索引完成时又把它写回去 —— 用户看到的是「删了没反应」。
#: 所以这些状态下直接拦住，等它变成 processed / failed 再删。
INDEXING_STATUSES = ("pending", "parsing", "analyzing", "processing", "preprocessed")


def _doc_statuses(doc_ids: List[str], *, max_pages: int = 3) -> Dict[str, str]:
    """查这些 doc_id 在上游的状态（翻几页就够；查不到就不拦，尽力而为）。

    只用于「别白删一次」的判断：全程只读，失败（上游不可用）时返回空表。
    """
    wanted = {str(item).strip() for item in doc_ids if str(item).strip()}
    if not wanted:
        return {}

    found: Dict[str, str] = {}
    for page in range(1, max(1, max_pages) + 1):
        payload = lightrag.documents_paginated(page=page, page_size=100)
        for item in payload.get("documents") or []:
            key = str(item.get("id") or "")
            if key in wanted:
                found[key] = str(item.get("status") or "").strip().lower()
        if len(found) >= len(wanted) or not (payload.get("pagination") or {}).get("has_next"):
            break
    return found


def _delete_message(status: int, body: str) -> str:
    """把「删除被拒」翻成人话（纯函数：不联网、不读盘，单测直接比对输出）。

    404 不在这里处理：调用方会把它当作「知识库早就没有了」，继续清理本机侧。
    """
    lowered = str(body or "").lower()
    if status == 409:
        if any(marker in lowered for marker in BUSY_MARKERS):
            return (
                "知识库正在处理别的文档（管线忙）：现在删会打断正在建的索引。"
                "等「知识库管线」显示空闲后再点一次删除即可（一般在几分钟内）。"
            )
        return "知识库拒绝了这次删除（HTTP 409）：多半是它正在删除同一批文档，稍等一会儿再试。"
    if status in (401, 403):
        return "知识库拒绝了这次访问：请检查它自己的鉴权配置（LightRAG 的 API Key / 白名单）。"
    return humanize_upstream(status, body)


def _delete_targets(
    payload: FilesDelete,
    index: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """把页面勾选的行解析成删除目标：`{doc_id, file_id, record, file_path, name}`。

    优先用 `items`（一行一个对象，doc_id 与 file_id 天然成对，页面就是这么发的）；
    只有在没给 items 时才用 `doc_ids` / `file_ids` / `file_paths` 这几条简化形式。
    """
    targets: List[Dict[str, Any]] = []
    all_records = [item for items in index.values() for item in items]
    seen_doc_ids: set = set()
    seen_file_ids: set = set()

    def add(item: Dict[str, Any]) -> None:
        record = item.get("record")
        if not record and item.get("file_id"):
            record = library.record_for(str(item["file_id"]), all_records)
        target = {
            "doc_id": str(item.get("doc_id") or "").strip(),
            "file_id": str(item.get("file_id") or "").strip(),
            "file_path": str(item.get("file_path") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "record": record,
        }
        if record:
            target["file_path"] = target["file_path"] or str(record.get("markdown_name") or "")
            target["name"] = target["name"] or str(record.get("name") or "")
        if not (target["doc_id"] or target["file_id"] or target["file_path"]):
            return
        if target["doc_id"]:
            seen_doc_ids.add(target["doc_id"])
        if target["file_id"]:
            seen_file_ids.add(target["file_id"])
        targets.append(target)

    for item in payload.items:
        add(item.model_dump() if hasattr(item, "model_dump") else dict(item))

    for doc_id in payload.doc_ids:
        text = str(doc_id or "").strip()
        if text and text not in seen_doc_ids:
            add({"doc_id": text})

    for file_id in payload.file_ids:
        text = str(file_id or "").strip()
        if text and text not in seen_file_ids:
            add({"file_id": text})

    for file_path in payload.file_paths:
        text = str(file_path or "").strip()
        if not text:
            continue
        record = library.match_document(text, index=index)
        file_id = str((record or {}).get("file_id") or "")
        if file_id and file_id in seen_file_ids:
            continue
        add({"file_path": text, "file_id": file_id, "record": record})

    return targets


def _remove_local(
    targets: List[Dict[str, Any]],
    *,
    scope: str,
    remove_originals: bool,
) -> Dict[str, Any]:
    """删本机侧的东西（**只在知识库确认收到删除请求之后调用**）。

    · scope=local/all：删 knowledge\\ 里这条作业的留档 + 整个 data\\parsed\\<file_id>\\；
    · remove_originals=True：再删原件留档（data\\originals 与 data\\uploads 里那一份）。
      原件默认**不删** —— 它是用户最贵的东西，不能跟着「清理记录」一起没了。
    """
    removed: Dict[str, Any] = {"knowledge": [], "parsed": [], "originals": [], "notes": []}

    for target in targets:
        record = target.get("record") or {}
        file_id = str(record.get("file_id") or target.get("file_id") or "").strip()
        if not file_id:
            continue

        if scope in ("local", "all"):
            archive = ingest.drop_knowledge_archive(file_id)
            if archive:
                removed["knowledge"].append(archive)
            directory = ingest.job_dir(file_id)
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
                removed["parsed"].append(origin.relative_path(directory))
                logger.info("已删除本机解析留档：%s", directory)

        if remove_originals:
            archived = origin.safe_path(record.get("archived_path"))
            if archived is not None:
                try:
                    archived.unlink()
                    removed["originals"].append(origin.relative_path(archived))
                    logger.warning("已删除原件留档：%s", archived)
                except OSError as exc:
                    removed["notes"].append(f"{origin.relative_path(archived)} 删不掉：{exc}")

    return removed


@router.post("/api/files/delete")
def files_delete(payload: FilesDelete, request: Request) -> Dict[str, Any]:
    """删除文档：知识库记录 / 本机留档 / 上游 md 拷贝，逐档可选。

    顺序（不可颠倒）：先在知识库确认收到删除（2xx 或「本来就没了」= 404），
    **然后**才动本机文件 —— 否则会出现「本机留档删了、知识库里还留着文档」的
    不一致（那正是 Phase 15.1 花力气修掉的那类问题）。
    """
    control.guard_action(request, payload.token)

    scope = str(payload.scope or "kb").strip().lower()
    if scope not in DELETE_SCOPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_scope",
                "message": "删除范围只能是 kb / local / all",
                "scopes": DELETE_SCOPES,
            },
        )
    if not payload.confirm:
        # 页面上的「二次确认」落到后端是一个显式标志：少了它，误发的请求不该动任何数据
        raise HTTPException(
            status_code=400,
            detail={"error": "confirm_required", "message": "缺少确认标志（confirm=true）"},
        )

    index = library.build_index()
    targets = _delete_targets(payload, index)
    if not targets:
        raise HTTPException(
            status_code=400,
            detail={"error": "nothing_selected", "message": "没有选中任何文件"},
        )

    # 本地行可能没有 doc_id（旧档案 / 上游还没刷新）：按文档名去上游反查一次
    for target in targets:
        if target["doc_id"] or not target["file_path"]:
            continue
        try:
            found = lightrag.documents_by_name(target["file_path"])
        except UpstreamError as exc:              # 反查不到不是错误：本机侧照删
            logger.warning("按名反查 doc_id 失败（继续按本机侧处理）：%s", exc.message)
            found = []
        if found:
            target["doc_id"] = str(found[0].get("id") or "")

    doc_ids = [str(item["doc_id"]) for item in targets if item["doc_id"]]
    upstream_note = ""
    if doc_ids:
        # 「还在建索引」的文档先拦住：真机实测对正在解析的文档发删除，上游回 200
        # 「deletion_started」，但索引完成时又把文档写回去 —— 用户看到「删了没反应」。
        try:
            statuses = _doc_statuses(doc_ids)
        except UpstreamError as exc:          # 查不到状态就不拦（尽力而为，不挡用户）
            logger.warning("查询文档状态失败（按不拦截处理）：%s", exc.message)
            statuses = {}
        indexing = [
            target
            for target in targets
            if statuses.get(str(target["doc_id"]), "") in INDEXING_STATUSES
        ]
        if indexing:
            names = "、".join(
                str(item.get("name") or item.get("file_path") or item["doc_id"]) for item in indexing[:3]
            )
            states = "、".join(
                sorted({library.upstream_status_label(statuses[str(item["doc_id"])]) for item in indexing})
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "kb_indexing",
                    "message": f"「{names}」还在建索引（{states}）：现在删会被正在进行的索引覆盖，"
                               "等于白删一次。等列表里显示「已完成」后再点删除即可（一般几分钟内）；"
                               "如果一直卡在同一个状态，请到 LightRAG 侧查看它的管线状态。",
                    "doc_ids": [item["doc_id"] for item in indexing],
                },
            )

        response = lightrag.delete_document(
            doc_ids,
            delete_file=(scope == "all"),
            delete_llm_cache=bool(payload.delete_llm_cache),
        )
        if response.status_code == 404:
            # 上游说「没有这条」= 目标已不存在：继续清理本机侧，并如实说明
            upstream_note = "知识库里已经没有这些文档了（可能刚被删过），本次只清理了本机留档。"
            doc_ids = []
        elif response.status_code >= 400:
            body = response.text[:500]
            message = _delete_message(response.status_code, body)
            logger.warning("知识库拒绝删除：HTTP %s | %s", response.status_code, message)
            raise HTTPException(
                status_code=409 if response.status_code == 409 else 502,
                detail={
                    "error": "delete_rejected",
                    "message": message,
                    "upstream_status": response.status_code,
                    "upstream_body": body,
                },
            )

    removed = _remove_local(targets, scope=scope, remove_originals=bool(payload.remove_originals))

    # 只删了知识库记录（留档还在）时，把这件事记进档案 —— 否则档案里那句
    # 「已入库」会一直骗人（state=done 会让它从「本机有、没入库」列表里消失）。
    if scope == "kb":
        for target in targets:
            file_id = str((target.get("record") or {}).get("file_id") or "")
            if file_id:
                ingest.mark_removed_from_kb(file_id)

    parts: List[str] = []
    if doc_ids:
        parts.append(f"知识库记录 {len(doc_ids)} 条")
    if removed["knowledge"]:
        parts.append(f"知识库留档 {len(removed['knowledge'])} 份")
    if removed["parsed"]:
        parts.append(f"本机解析留档 {len(removed['parsed'])} 份")
    if removed["originals"]:
        parts.append(f"原件留档 {len(removed['originals'])} 份")
    message = "已提交删除：" + ("、".join(parts) if parts else "没有需要删除的内容")
    if scope != "all" and doc_ids:
        message += "（上游 input 目录里的 md 拷贝保留）"
    if not payload.remove_originals and removed["parsed"]:
        message += "；原件保留在 data\\originals 与 data\\uploads"

    logger.warning(
        "用户删除文档：范围=%s，知识库=%s 条，本机留档=%s，原件=%s",
        scope,
        len(doc_ids),
        len(removed["knowledge"]) + len(removed["parsed"]),
        len(removed["originals"]),
    )
    return {
        "success": True,
        "scope": scope,
        "scope_label": DELETE_SCOPES[scope],
        "deleted_from_kb": doc_ids,
        "removed": removed,
        "message": message,
        "note": upstream_note or (
            "知识库的删除是异步的：它会在后台清理向量与图谱关系，稍后刷新列表即可看到记录消失"
            "（页面会自动再刷新两次）。"
        ),
        "async": True,
    }




