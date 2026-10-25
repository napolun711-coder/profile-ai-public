"""P0：OCR 工具适配器（包装 app.services.ocr.run_ocr）。

对等约束：`ToolResult.data` 就是原来的 `OcrOutcome` 对象。
外发边界：云端提供方（paddle）会把文件发到本机之外，所以 `external=True`、
`risk="medium"`，且默认**不允许 Skill 直接调用**（P1 的权限门再显式放开）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from app.core.config import get_settings
from app.plugins.contracts import (
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_OCR_FAILED,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from app.services import ocr as ocr_service

SPEC = ToolSpec(
    name="ocr.recognize",
    version="0.2.0",
    description="对本地文件执行 OCR（按配置选择提供方；云端提供方会把文件发到本机之外）",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "本地文件路径"},
            "filename": {"type": "string", "description": "展示名；缺省使用 path.name"},
            "content_type": {"type": "string", "description": "MIME 类型（可留空）"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "text": {"type": "string"},
            "pages": {"type": "integer"},
            "total_pages": {"type": "integer"},
            "images": {"type": "integer"},
            "job_id": {"type": "string"},
            "model": {"type": "string"},
            "provider": {"type": "string"},
            "elapsed_s": {"type": "number"},
            "detail": {"type": "string"},
        },
    },
    capabilities=("ocr", "text_extraction", "image_to_text"),
    side_effect="read_local",
    external=True,
    risk="medium",
    timeout_s=600.0,
    retry=RetryPolicy(max_attempts=1),
    allowed_for_skills=False,
)


class OcrRecognizeTool:
    """把 `run_ocr()` 收敛成统一 Tool 返回；失败不抛异常。"""

    spec = SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        path: Any,
        filename: str = "",
        content_type: str = "",
        on_progress: Any = None,
    ) -> ToolResult:
        started = time.perf_counter()

        if filename is None:
            filename = ""
        if not isinstance(filename, str) or not isinstance(content_type, str):
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INVALID_INPUT,
                    message="filename / content_type 必须是字符串",
                ),
                usage={"elapsed_ms": _elapsed_ms(started)},
            )

        try:
            target = Path(path)
        except (TypeError, ValueError) as exc:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INVALID_INPUT,
                    message=f"path 不是有效路径：{exc}",
                ),
                usage={"elapsed_ms": _elapsed_ms(started)},
            )

        settings = ctx.settings if ctx is not None and ctx.settings is not None else get_settings()
        try:
            outcome = ocr_service.run_ocr(
                target,
                filename or target.name,
                content_type=content_type,
                settings=settings,
                on_progress=on_progress,
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INTERNAL,
                    message=f"OCR 工具异常（{type(exc).__name__}）：{exc}",
                ),
                usage={"elapsed_ms": _elapsed_ms(started)},
            )

        usage = {
            "elapsed_ms": _elapsed_ms(started),
            "elapsed_s": outcome.elapsed_s,
            "provider": outcome.provider,
            "model": outcome.model,
            "pages": outcome.pages,
            "job_id": outcome.job_id,
        }
        if not outcome.ok:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_OCR_FAILED,
                    message=outcome.detail or "OCR 失败",
                    detail=outcome.detail,
                ),
                data=outcome,
                usage=usage,
            )
        return ToolResult.success(outcome, usage=usage)

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        # 只看配置，不发请求；真正的提供方探活由设置页的 /api/settings/test 负责。
        settings = ctx.settings if ctx is not None and ctx.settings is not None else get_settings()
        if bool(getattr(settings, "ocr_enabled", False)):
            provider = str(getattr(settings, "ocr_provider", "") or "")
            return Health(ok=True, detail=f"OCR 已启用（provider={provider}）")
        return Health(ok=False, detail="OCR 未启用（OCR_MODE=off 或提供方/令牌不可用）")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


TOOL = OcrRecognizeTool()
