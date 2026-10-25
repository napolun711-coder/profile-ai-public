"""P0：本地解析工具适配器（包装 app.services.parse.parse_file）。

对等约束：`ToolResult.data` 就是原来的 `ParseResult` 对象，字段不裁剪、不改名。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from app.plugins.contracts import (
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_PARSE_FAILED,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from app.services import parse as parse_service

SPEC = ToolSpec(
    name="parse.document",
    version="0.2.0",
    description="把本地文件解析为 Markdown 文本与元数据（纯本地，不联网）",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "本地文件路径"},
            "filename": {
                "type": "string",
                "description": "展示名；缺省使用 path.name",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "route": {"type": "string"},
            "text": {"type": "string"},
            "pages": {"type": "integer"},
            "images": {"type": "integer"},
            "parser": {"type": "string"},
            "detail": {"type": "string"},
            "truncated": {"type": "boolean"},
            "assets": {"type": "integer"},
        },
    },
    capabilities=("document_parse", "text_extraction"),
    side_effect="read_local",
    external=False,
    risk="low",
    timeout_s=120.0,
    retry=RetryPolicy(max_attempts=1),
    allowed_for_skills=True,
)


class ParseDocumentTool:
    """把 `parse_file()` 收敛成统一 Tool 返回；失败不抛异常。"""

    spec = SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        path: Any,
        filename: Optional[str] = None,
    ) -> ToolResult:
        started = time.perf_counter()

        if filename is not None and not isinstance(filename, str):
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INVALID_INPUT,
                    message="filename 必须是字符串或 None",
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

        try:
            result = parse_service.parse_file(target, filename)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INTERNAL,
                    message=f"解析工具异常（{type(exc).__name__}）：{exc}",
                ),
                usage={"elapsed_ms": _elapsed_ms(started)},
            )

        usage = {
            "elapsed_ms": _elapsed_ms(started),
            "route": result.route,
            "pages": result.pages,
            "chars": result.char_total,
        }
        if not result.ok:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_PARSE_FAILED,
                    message=result.detail or "解析失败",
                    detail=result.detail,
                ),
                data=result,
                usage=usage,
            )
        return ToolResult.success(result, usage=usage)

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        # 本地解析没有外部依赖；这里只做静态声明，不读磁盘。
        return Health(ok=True, detail="本地解析可用（无外部依赖）")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


TOOL = ParseDocumentTool()
