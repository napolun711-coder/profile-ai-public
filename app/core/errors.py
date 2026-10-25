"""统一错误处理（Phase 3）。

把上游（LightRAG）故障统一转换为带明确含义的 JSON 响应，避免把堆栈抛给前端：
    502 upstream_unavailable  上游不可用（连接被拒绝 / DNS 失败等）
    504 upstream_timeout      上游超时
    500 internal_error        未预期异常兜底

Phase 15.1 起还负责**翻译上游的业务拒绝**：`humanize_upstream()` 把 LightRAG 的英文
报错（例如「Document storage already contains '…' 」）翻成用户看得懂、且知道下一步
做什么的一句话。它是**纯函数**（不发请求、不读磁盘），原始返回仍然完整保留在
result.json 的 `lightrag.error` 里，网页上可展开查看原文。

说明：正常路径的响应结构与 Phase 2 完全一致，本模块只影响失败路径。
"""

from __future__ import annotations

import json
import logging
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")

UPSTREAM_UNAVAILABLE = "upstream_unavailable"
UPSTREAM_TIMEOUT = "upstream_timeout"
INTERNAL_ERROR = "internal_error"

BODY_PREVIEW_LIMIT = 500
#: 翻译后单句里保留的上游原文长度（再长就截断，完整原文仍在档案里）
MESSAGE_PREVIEW_LIMIT = 160

#: 上游报错里出现这些词 → 属于「同名文档已存在」这类可解释的拒绝（重复上传）
DUPLICATE_MARKERS = (
    "already contains",
    "already exists",
    "duplicate",
    "already been added",
)

_DUPLICATE_NAME_RE = re.compile(r"already contains\s+'([^']+)'", re.IGNORECASE)
_DUPLICATE_STATUS_RE = re.compile(r"\(\s*status\s*:\s*([^)]+)\)", re.IGNORECASE)


def _flatten(text: str) -> str:
    """把多行文本压成单行（上报给前端的是短句，不是堆栈）。"""
    return " ".join(str(text or "").split())


def _clean_upstream_text(text: str) -> str:
    """把上游返回的 JSON / 文本压成一句可读的话（去转义与引号并截断）。"""
    flat = _flatten(text).strip().strip('"').strip("'")
    if len(flat) > MESSAGE_PREVIEW_LIMIT:
        flat = flat[: MESSAGE_PREVIEW_LIMIT - 1] + "…"
    return flat


def upstream_message(body: str = "") -> str:
    """从上游响应体里抽出人话：优先 detail / message / error 字段，其次原始文本。"""
    text = str(body or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return _clean_upstream_text(text)
    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_upstream_text(value)
        # 上游有时把原因包在嵌套对象里：能压成一行就压，压不动就整体截断
        return _clean_upstream_text(json.dumps(payload, ensure_ascii=False))
    return _clean_upstream_text(text)


def _duplicate_name(message: str) -> str:
    found = _DUPLICATE_NAME_RE.search(message)
    return found.group(1).strip() if found else ""


def _duplicate_status(message: str) -> str:
    found = _DUPLICATE_STATUS_RE.search(message)
    return found.group(1).strip() if found else ""


def humanize_upstream(status_code: int, body: str = "") -> str:
    """把「知识库拒绝接收」翻成用户看得懂、且知道下一步做什么的一句话。

    设计约束（与 services/advice.py 同样按纯函数对待）：
    1. 不发请求、不读磁盘 —— 单测直接比对输出（tests/test_upstream_messages.py）；
    2. 结论必须可解释：先说是哪一类问题，再说用户能做什么；
    3. 原文不丢：翻好后仍附一小段原始返回（完整原文在 result.json 的 lightrag.error）。
    """
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = 0

    message = upstream_message(body)
    lowered = message.lower()

    if any(marker in lowered for marker in DUPLICATE_MARKERS):
        name = _duplicate_name(message)
        state = _duplicate_status(message)
        head = "知识库中已经有同名文档" + (f"「{name}」" if name else "")
        if state:
            head += f"（状态 {state}）"
        text = (
            head
            + "：这次没有写入新文档，属于重复上传。想用新版本替换，请先在知识库页面删掉旧记录，"
            + "再点「重试」；这份不需要的话点「跳过」即可。"
        )
        return text + (f" 知识库原文：{message}" if message else "")

    if status == 413:
        return "知识库拒绝接收：文件太大（HTTP 413），可以拆分后再上传。"
    if status == 415:
        return "知识库不支持这种文件类型（HTTP 415）：请改用 txt / md / pdf 等常见格式。"
    if status in (401, 403):
        return (
            f"知识库拒绝了这次访问（HTTP {status}）：请检查 LightRAG 的鉴权配置"
            "（例如设置页/`.env` 里的 LIGHTRAG_API_KEY）。"
        )
    if status == 429:
        return "知识库当前太忙（HTTP 429）：稍等一下再点「重试」即可。"

    if status >= 500:
        base = f"知识库服务内部错误（HTTP {status}）：属于服务端临时故障，稍后再点「重试」。"
    elif status == 409:
        base = "知识库拒绝了这次提交（HTTP 409）：可能已存在同名或同内容的文档，请先在知识库页面确认。"
    elif status == 0:
        base = "知识库没有正常应答：这次提交的结果未知，请点「重试」确认。"
    else:
        base = f"知识库拒绝了这次提交（HTTP {status}）。"
    return base + (f" 知识库原文：{message}" if message else "")


class UpstreamError(Exception):
    """上游服务异常（连接失败 / 超时 / 上游返回错误状态码）。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = UPSTREAM_UNAVAILABLE,
        status_code: int = 502,
        upstream_status: int | None = None,
        upstream_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body

    def to_detail(self) -> dict:
        detail = {
            "error": self.kind,
            "message": self.message,
        }
        if self.upstream_status is not None:
            detail["upstream_status"] = self.upstream_status
        if self.upstream_body:
            detail["upstream_body"] = self.upstream_body[:BODY_PREVIEW_LIMIT]
        return detail


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器（UpstreamError + 兜底 Exception）。

    注意：RequestValidationError（422）沿用 FastAPI 默认处理，
    以保持既有响应结构不变。
    """

    @app.exception_handler(UpstreamError)
    async def _handle_upstream_error(request: Request, exc: UpstreamError) -> JSONResponse:
        logger.error(
            "上游异常 %s %s -> %s | %s",
            request.method,
            request.url.path,
            exc.kind,
            exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_detail()})

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Starlette 的 ServerErrorMiddleware 在返回本响应后仍会把异常抛给 uvicorn
        # 记录堆栈（便于排障）；但客户端拿到的是结构化 JSON，而非堆栈文本。
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "error": INTERNAL_ERROR,
                    "message": "服务内部错误，请查看 logs/app.log",
                }
            },
        )