"""OCR 服务（Phase 9）：provider 化的统一入口。

对外只暴露三样东西（其余都是内部实现）：
    run_ocr(path, filename, *, content_type, settings, on_progress) -> OcrOutcome
    OcrOutcome            统一的识别结果（失败时 detail 是中文原因）
    get_provider(settings) / provider_infos(settings)  设置页与探活用

合规约束：
1. `OCR_MODE=off`（默认）时不会发起任何网络请求，也不会读写任何 OCR 服务；
2. 只有 `OCR_PROVIDER=paddle` 会把文件发送到本机之外（`settings.ocr_external` 为真时界面必须提示）；
3. 兼容旧名：`app.services.ocr.ocr_available(settings)` 等价于 `settings.ocr_enabled`。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.services.ocr.base import OcrOutcome, OcrProvider, brief, http_failure
from app.services.ocr.registry import (
    NONE_PROVIDER,
    PROVIDERS,
    get_provider,
    provider_infos,
    run_with_provider,
)

__all__ = [
    "OcrOutcome",
    "OcrProvider",
    "PROVIDERS",
    "NONE_PROVIDER",
    "brief",
    "get_provider",
    "get_settings",
    "http_failure",
    "ocr_available",
    "provider_infos",
    "run_ocr",
]


def ocr_available(settings=None) -> bool:
    """（兼容旧名）当前配置下是否允许调用 OCR。"""
    settings = settings or get_settings()
    return bool(settings.ocr_enabled)


def ocr_external(settings=None) -> bool:
    """当前配置下 OCR 是否会把文件发送到本机之外。"""
    settings = settings or get_settings()
    return bool(getattr(settings, "ocr_external", False))


def run_ocr(
    path: Path,
    filename: str,
    *,
    content_type: str = "",
    settings=None,
    on_progress=None,
) -> OcrOutcome:
    """对本地文件执行一次 OCR（自动选择提供方）；失败不抛异常。"""
    settings = settings or get_settings()

    if not settings.ocr_enabled:
        return OcrOutcome(
            ok=False,
            provider=str(getattr(settings, "ocr_provider", "") or ""),
            detail=(
                "OCR 未启用：请在设置页（或 .env）把 OCR_MODE 设为 manual/auto，"
                "并选择可用的 OCR 提供方"
            ),
        )

    provider = get_provider(settings)
    return run_with_provider(
        provider,
        path,
        filename,
        content_type=content_type,
        settings=settings,
        on_progress=on_progress,
    )
