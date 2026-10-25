"""OCR 提供方抽象（Phase 9）。

设计要点：
1. **所有 OCR 实现都必须实现这里定义的 `OcrProvider`**，上层（ingest.py）只依赖
   `run_ocr()` 与 `OcrOutcome`，不关心是云端还是本机；
2. `external=True` 的提供方会在运行期把文件发送到本机之外（界面上必须明确提示）；
3. 任何失败都返回 `OcrOutcome(ok=False, detail=中文原因)`：不把异常抛给上层；
4. 错误分级（云端）：401/403/413/429 不重试；5xx 与网络错误重试 SUBMIT_RETRIES 次。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SUBMIT_RETRIES = 2
RETRY_BACKOFF = 3.0
RETRY_STATUS = (500, 502, 503, 504)
POLL_READ_TIMEOUT = 60
SUBMIT_READ_TIMEOUT = 120
RESULT_READ_TIMEOUT = 180

# 可识别的文件后缀（提供方可以据此拒绝不支持的格式）
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


@dataclass
class OcrOutcome:
    """OCR 结果：ok=False 时 detail 为可以直接展示给用户的中文原因。"""

    ok: bool
    text: str = ""
    pages: int = 0
    total_pages: int = 0
    images: int = 0
    job_id: str = ""
    model: str = ""
    provider: str = ""
    elapsed_s: float = 0.0
    detail: str = ""


def brief(text: str, limit: int = 300) -> str:
    """把上游返回体压成一行，便于放进用户可见的原因里。"""
    return (text or "").replace("\n", " ")[:limit]


def http_failure(status: int, body: str) -> Tuple[bool, str]:
    """把 HTTP 失败翻译为 (是否可重试, 中文原因)。"""
    if status in (401, 403):
        return False, f"OCR 令牌无效或已过期（HTTP {status}），请在设置页更新 OCR Token"
    if status == 413:
        return False, "文件超过 OCR 服务的体积限制（HTTP 413），请拆分后重试"
    if status == 429:
        return False, "OCR 当日配额已用尽（HTTP 429），请次日再试或改用本地解析"
    if status in RETRY_STATUS:
        return True, f"OCR 服务暂时不可用（HTTP {status}）：{brief(body)}"
    return False, f"OCR 服务返回错误（HTTP {status}）：{brief(body)}"


class OcrProvider:
    """OCR 提供方基类（无状态、可复用）。"""

    name: str = "base"          # 配置值（OCR_PROVIDER）
    label: str = "OCR"          # 界面展示名
    external: bool = False      # 是否会把文件发往本机之外
    needs_token: bool = False   # 是否需要令牌

    def available(self, settings) -> bool:
        """当前配置下是否可以调用（模式 + 凭据 + 依赖）。"""
        return False

    def unavailable_reason(self, settings) -> str:
        """不可用时的中文原因（供界面展示）。"""
        return "未启用"

    def run(
        self,
        path: Path,
        filename: str,
        *,
        content_type: str = "",
        settings=None,
        on_progress=None,
    ) -> OcrOutcome:  # pragma: no cover - 抽象方法
        raise NotImplementedError

    def describe(self, settings) -> Dict[str, Any]:
        """给设置页用的一段说明（不含任何密钥）。"""
        available = self.available(settings)
        return {
            "name": self.name,
            "label": self.label,
            "external": self.external,
            "needs_token": self.needs_token,
            "available": available,
            "detail": "" if available else self.unavailable_reason(settings),
        }

    # ---------- 便捷方法 ----------

    @staticmethod
    def started() -> float:
        return time.monotonic()

    @staticmethod
    def fail(
        detail: str,
        *,
        provider: str = "",
        model: str = "",
        started: float = 0.0,
        job_id: str = "",
        total_pages: int = 0,
    ) -> OcrOutcome:
        return OcrOutcome(
            ok=False,
            detail=detail,
            provider=provider,
            model=model,
            job_id=job_id,
            total_pages=total_pages,
            elapsed_s=(time.monotonic() - started) if started else 0.0,
        )


def check_file_size(path: Path, settings, *, provider: str, model: str = "") -> Optional[OcrOutcome]:
    """通用前置检查：文件存在且不超过 OCR_MAX_FILE_MB；返回 None 表示通过。"""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return OcrOutcome(ok=False, provider=provider, model=model, detail=f"读取文件失败：{exc}")

    limit_mb = int(getattr(settings, "ocr_max_file_mb", 0) or 0)
    if limit_mb > 0 and size > limit_mb * 1024 * 1024:
        return OcrOutcome(
            ok=False,
            provider=provider,
            model=model,
            detail=(
                f"文件 {size / 1048576:.1f} MB 超过 OCR 上限 {limit_mb} MB，"
                "请拆分后重试（或调大 OCR_MAX_FILE_MB）"
            ),
        )
    return None
