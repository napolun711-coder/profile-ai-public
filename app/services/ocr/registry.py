"""OCR 提供方注册表（Phase 9）：

- `PROVIDERS`   名称 → 提供方实例（`OCR_PROVIDER` 的合法取值）
- `get_provider(settings)`  按配置取提供方（未知名称回落到一个"永不就绪"的空实现）
- `provider_infos(settings)` 给设置页用的说明清单（不含任何密钥）
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.core.config import get_settings
from app.services.ocr.base import OcrOutcome, OcrProvider
from app.services.ocr.providers import local_paddle, paddle_jobs, stub

PROVIDERS: Dict[str, OcrProvider] = {
    paddle_jobs.PROVIDER.name: paddle_jobs.PROVIDER,
    local_paddle.PROVIDER.name: local_paddle.PROVIDER,
    stub.PROVIDER.name: stub.PROVIDER,
}

# 设置页展示顺序（先云端后本机，便于用户理解"哪个会外发"）
DISPLAY_ORDER = ("paddle", "local", "stub")


class DisabledProvider(OcrProvider):
    """未知/禁用提供方：始终不可用（保证配置写错时不会静默外发）。"""

    name = "none"
    label = "未启用"
    external = False

    def unavailable_reason(self, settings) -> str:
        return "OCR 已明确禁用（OCR_PROVIDER=none 或取值无法识别）"


NONE_PROVIDER = DisabledProvider()


def get_provider(settings=None) -> OcrProvider:
    """按 OCR_PROVIDER 取提供方；未配置或取值非法时返回"禁用"实现。"""
    settings = settings or get_settings()
    name = str(getattr(settings, "ocr_provider", "") or "").strip().lower()
    return PROVIDERS.get(name, NONE_PROVIDER)


def provider_infos(settings=None) -> List[Dict[str, Any]]:
    """设置页用的提供方清单（含 selected 标记）。"""
    settings = settings or get_settings()
    selected = str(getattr(settings, "ocr_provider", "") or "").strip().lower()
    infos: List[Dict[str, Any]] = []
    for name in DISPLAY_ORDER:
        provider = PROVIDERS.get(name)
        if provider is None:
            continue
        info = provider.describe(settings)
        info["selected"] = name == selected
        infos.append(info)
    return infos


def run_with_provider(
    provider: OcrProvider,
    path,
    filename: str,
    *,
    content_type: str = "",
    settings=None,
    on_progress=None,
) -> OcrOutcome:
    """统一入口：把提供方的异常兜底成 OcrOutcome（任何情况都不抛给上层）。"""
    import logging

    logger = logging.getLogger(__name__)
    try:
        return provider.run(
            path,
            filename,
            content_type=content_type,
            settings=settings,
            on_progress=on_progress,
        )
    except Exception as exc:  # noqa: BLE001 - OCR 失败不能影响入库流程的可解释性
        logger.exception("OCR 提供方异常：%s", provider.name)
        return OcrOutcome(
            ok=False,
            provider=provider.name,
            detail=f"OCR 提供方内部错误（{type(exc).__name__}）：{exc}",
        )
