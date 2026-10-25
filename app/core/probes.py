"""轻量探活工具（Phase 3）。

只做 TCP 连通性检查（socket），不引入新依赖、不产生业务副作用，
用于 GET /health 汇总 Qdrant / Ollama 等外部依赖状态。
"""

from __future__ import annotations

import socket
from typing import Any, Dict
from urllib.parse import urlparse


def probe_tcp(url: str, timeout: float = 3.0) -> Dict[str, Any]:
    """检查 url 对应的 主机:端口 是否能建立 TCP 连接。"""
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    result: Dict[str, Any] = {
        "url": url,
        "host": host,
        "port": port,
        "ok": False,
        "detail": None,
    }

    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["ok"] = True
    except OSError as exc:
        result["detail"] = exc.__class__.__name__

    return result