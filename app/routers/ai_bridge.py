"""Batch 2：本地 AI 桥接（AHK → 本应用）。

边界（与项目记忆规则一致）：
- 只做**内存中转**：AHK 把用户在 ChatGPT 里复制的一段文本 POST 进来，
  前端轮询取走并填入输入框；不落盘、不写 `app.memory`、不生成记忆候选。
- 日志只记长度/状态，不记正文。
- 默认 `AI_BRIDGE_ENABLED=off`；关闭时 status 返回 enabled=false，
  ingest/pending 一律 404。
- 鉴权：进程内随机 token。AHK 从 `/status` 取 token，请求带
  `X-AI-Bridge-Token`；status 不做鉴权，靠同源/CORS 防止跨站读取。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

#: 进程内随机 token；重启即失效。AHK 收到 403 后会重新读取 /status。
_TOKEN = secrets.token_urlsafe(24)

_LOCK = threading.Lock()
_PENDING: Dict[str, Any] = {}
_LAST_HASH = ""
_LAST_AT = 0.0
_DEDUPE_WINDOW_SECONDS = 5.0


def _enabled() -> bool:
    try:
        return bool(get_settings().ai_bridge_enabled)
    except Exception:  # noqa: BLE001 - 配置异常按关闭处理，不能拖垮聊天
        return False


def _ttl_seconds() -> int:
    try:
        return max(5, int(get_settings().ai_bridge_ttl_seconds))
    except Exception:  # noqa: BLE001
        return 300


def _max_chars() -> int:
    try:
        return max(256, int(get_settings().ai_bridge_max_chars))
    except Exception:  # noqa: BLE001
        return 65536


def _auto_send() -> bool:
    try:
        return bool(getattr(get_settings(), "ai_bridge_auto_send", False))
    except Exception:  # noqa: BLE001
        return False


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=404, detail="AI bridge disabled")


def _require_token(request: Request) -> None:
    supplied = str(request.headers.get("x-ai-bridge-token") or "")
    if not supplied or not secrets.compare_digest(supplied, _TOKEN):
        raise HTTPException(status_code=403, detail="invalid AI bridge token")


@router.get("/api/ai-bridge/status")
def status() -> Dict[str, Any]:
    """前端/AHK 读取开关与本次进程的 token。"""
    enabled = _enabled()
    return {
        "enabled": enabled,
        "token": _TOKEN if enabled else "",
        "ttl_seconds": _ttl_seconds(),
        "max_chars": _max_chars(),
        "auto_send": _auto_send(),
    }


@router.post("/api/ai-bridge/ingest")
async def ingest(request: Request) -> Dict[str, Any]:
    """AHK 回传一段文本。请求体是 UTF-8 纯文本，避免在 AHK 里手写 JSON。"""
    global _LAST_HASH, _LAST_AT

    _require_enabled()
    _require_token(request)

    content_type = str(request.headers.get("content-type") or "").lower()
    if not content_type.startswith("text/plain"):
        raise HTTPException(status_code=415, detail="content-type must be text/plain")

    body = await request.body()
    max_chars = _max_chars()
    if len(body) > max_chars * 4:  # UTF-8 每字符最多 4 字节，先按字节挡一次
        raise HTTPException(status_code=413, detail="text too long")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="body must be UTF-8") from exc

    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > max_chars:
        raise HTTPException(status_code=413, detail="text too long")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _LOCK:
        if _LAST_HASH == digest and (now - _LAST_AT) < _DEDUPE_WINDOW_SECONDS:
            return {"ok": True, "duplicate": True, "id": ""}
        _LAST_HASH = digest
        _LAST_AT = now
        item_id = secrets.token_hex(8)
        # 只保留最新一条；旧条目未取走时被新条目覆盖，避免堆积正文。
        _PENDING.clear()
        _PENDING.update({"id": item_id, "text": text, "created": now})

    logger.info("ai_bridge ingest chars=%d", len(text))
    return {"ok": True, "duplicate": False, "id": item_id}


@router.get("/api/ai-bridge/pending")
async def pending(request: Request):
    """前端取走一条待填入文本；取走即消费。"""
    _require_enabled()
    _require_token(request)

    now = time.monotonic()
    with _LOCK:
        if not _PENDING:
            return Response(status_code=204)
        if (now - float(_PENDING.get("created") or 0.0)) > _ttl_seconds():
            _PENDING.clear()
            return Response(status_code=204)
        item = dict(_PENDING)
        _PENDING.clear()
    return item


def reset_for_tests() -> None:
    """测试隔离：清空内存条目与去重状态。"""
    global _LAST_HASH, _LAST_AT
    with _LOCK:
        _PENDING.clear()
        _LAST_HASH = ""
        _LAST_AT = 0.0
