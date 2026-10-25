"""P2C：本地图片检索工具（只读，包装 `services.image_evidence`）。

与 `knowledge.search` 的分工：
- `knowledge.search` 走 LightRAG `/query/data`，是外部检索通道；
- `image.search` 只读本机 gallery 索引，是图片证据的本地通道；
- 两者在 `image_lookup` 技能里按配额合并。

边界：
- **不加入 BUILTIN_TOOLS**：`/api/tools` 与设置页保持原样，只有影子脚本/测试显式注册；
- 只读：不调用 `gallery.rebuild()` / `find_or_rebuild()`，不写索引、不写配置；
- 不联网、不调用 OCR/LightRAG；
- 审计由 ToolManager 统一写，只留参数键名与哈希，不落问题原文。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from app.plugins.contracts import (
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from app.services import gallery, image_evidence

SPEC = ToolSpec(
    name="image.search",
    version="0.1.0",
    description="只读检索本机图片资料索引（caption / OCR 预览 / 关键词）",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "问题（至少 3 个字符）"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": image_evidence.MAX_LIMIT,
                "description": f"最多返回条数（默认 {image_evidence.DEFAULT_LIMIT}）",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "matched": {"type": "integer"},
            "returned": {"type": "integer"},
            "truncated": {"type": "boolean"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "doc_name": {"type": "string"},
                        "file_id": {"type": "string"},
                        "image_id": {"type": "string"},
                        "score": {"type": "number"},
                        "origin_url": {"type": "string"},
                    },
                },
            },
        },
    },
    capabilities=("retrieval", "image_search", "read_only"),
    side_effect="read_local",
    external=False,
    risk="low",
    timeout_s=30.0,
    retry=RetryPolicy(),
    allowed_for_skills=True,
)


class ImageSearchTool:
    """把本地图片证据检索收敛成统一 Tool 返回；失败不抛异常。"""

    spec = SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        query: Any,
        limit: Any = None,
    ) -> ToolResult:
        started = time.perf_counter()
        invalid = _validate(query, limit)
        if invalid is not None:
            return ToolResult.failure(
                invalid, usage={"elapsed_ms": _elapsed_ms(started)}
            )

        text = query.strip()
        size = image_evidence.DEFAULT_LIMIT if limit is None else int(limit)
        try:
            evidence, stats = image_evidence.search_local(text, limit=size)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INTERNAL,
                    message=f"图片检索工具异常（{type(exc).__name__}）",
                    detail=str(exc),
                ),
                usage={"elapsed_ms": _elapsed_ms(started), "query_chars": len(text)},
            )

        items = [
            {
                "evidence_id": item.evidence_id,
                "doc_name": item.doc_name,
                "file_id": item.file_id,
                "image_id": item.image_id,
                "score": float(item.score) if isinstance(item.score, (int, float)) else 0.0,
                "origin_url": item.origin_url,
            }
            for item in evidence
        ]
        data: Dict[str, Any] = {
            "total": int(stats.get("total") or 0),
            "matched": int(stats.get("matched") or 0),
            "returned": int(stats.get("returned") or 0),
            "truncated": bool(stats.get("truncated")),
            "items": items,
        }
        usage = {
            "elapsed_ms": _elapsed_ms(started),
            "query_chars": len(text),
            "limit": size,
            "total": data["total"],
            "matched": data["matched"],
            "returned": data["returned"],
            "truncated": data["truncated"],
            "reason": str(stats.get("reason") or ""),
        }
        return ToolResult.success(data, usage=usage, evidence=list(evidence))

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        """只检查索引可读性；不触发 rebuild、不写盘。"""
        started = time.perf_counter()
        try:
            path = gallery.index_path()
            if not path.is_file():
                return Health(
                    ok=False,
                    detail="本地图片索引不存在（本地通道为空）",
                    latency_ms=_elapsed_ms(started),
                )
            # 严格解析：`gallery.read_index()` 会把坏 JSON 降级成空索引，
            # 探活不能沿用它，否则损坏的索引会被误报成“可读”。
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
                raise ValueError("索引结构缺少 items 字典")
            return Health(
                ok=True,
                detail="本地图片索引可读",
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:  # noqa: BLE001 - 探活失败也返回结构化结果
            return Health(
                ok=False,
                detail=f"本地图片索引损坏或不可读（{type(exc).__name__}）",
                latency_ms=_elapsed_ms(started),
            )


def _validate(query: Any, limit: Any) -> Optional[ToolError]:
    if not isinstance(query, str) or len(query.strip()) < 3:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message="query 必须是至少 3 个字符的字符串",
        )
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int):
        return ToolError(code=ERROR_INVALID_INPUT, message="limit 必须是整数")
    if limit < 1 or limit > image_evidence.MAX_LIMIT:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message=f"limit 必须在 1～{image_evidence.MAX_LIMIT} 之间",
        )
    return None


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


TOOL = ImageSearchTool()

__all__ = ["ImageSearchTool", "SPEC", "TOOL"]
