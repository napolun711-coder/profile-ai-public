"""P3B：本机生成版 `knowledge.answer` 工具（只读，技能路由专用）。

只在 `GENERATION_MODE=local` 时由 `app/skills/production.build_runtime()` 注册；
工具名与上游版相同（`knowledge.answer`），因此 `knowledge_qa` 技能、工具策略与
`ENABLED_TOOLS` 都不需要改。

边界：
- 调用 P3A/P3B 的生产门面 `local_generation.answer()`，不直接碰 LightRAG / Ollama；
- 失败只返回 `ToolError`（message 不含问题、提示词、证据或回答正文），
  由 `knowledge_qa` → `production.SkillRuntime` → `/chat` 逐层回退上游；
- 成功时 `data` 仍是 `{response, references, response_time}`；
- 证据携带 ContextBuilder 的原始 label，供 `knowledge_qa` 映射成 `[img-n]` 等卡片。

P3C：`ToolContext.emit` 存在时走 `local_generation.iter_stream()`，
把 `references` / `chunk` 事件转发给调用方；生成结束后仍返回同一个 ToolResult。
已输出内容后的失败错误码带 `_streamed` 后缀，调用方不能再回退上游。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError
from app.plugins.contracts import (
    ERROR_INVALID_INPUT,
    ERROR_UPSTREAM,
    RETRYABLE_UPSTREAM_STATUS,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)
from app.services import local_generation

SPEC = ToolSpec(
    name="knowledge.answer",
    version="0.2.0",
    description="只读回答知识库问题（本机 Ollama 生成，引用仍来自 LightRAG 检索）",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "问题（至少 3 个字符）"},
            "conversation_history": {
                "type": "array",
                "description": "近期对话（本机 messages 历史）",
            },
            "user_prompt": {
                "type": "string",
                "description": "长期记忆/使用规则（放入本机 user 消息前缀）",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "response": {"type": "string"},
            "references": {"type": "array"},
            "response_time": {"type": "number"},
        },
    },
    capabilities=("answer", "knowledge_qa", "local_generation", "read_only"),
    side_effect="none",
    # 本机 Ollama 仍是进程外服务；生产策略允许 external=True。
    external=True,
    risk="low",
    timeout_s=420.0,
    # 生成本身不做重试：重试会重复消耗数分钟，且失败后已经有上游回退。
    retry=RetryPolicy(),
    allowed_for_skills=True,
)


class LocalAnswerTool:
    """把 `local_generation.answer()` 收敛成统一 Tool 返回；失败不抛异常。"""

    spec = SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        query: Any,
        conversation_history: Any = None,
        user_prompt: Any = None,
    ) -> ToolResult:
        started = time.perf_counter()
        invalid = _validate_query(query)
        if invalid is not None:
            return ToolResult.failure(invalid, usage=_usage(started, None, None))

        text = query.strip()
        history = (
            conversation_history
            if isinstance(conversation_history, (list, tuple))
            else None
        )
        memory_text = user_prompt if isinstance(user_prompt, str) else ""
        emit = getattr(ctx, "emit", None) if ctx is not None else None
        if emit is not None:
            return self._invoke_stream(
                text,
                emit,
                getattr(ctx, "cancelled", None) if ctx is not None else None,
                started,
                history=history,
                memory_text=memory_text,
            )
        try:
            kwargs = {}
            if history:
                kwargs["history"] = history
            if memory_text:
                kwargs["memory_text"] = memory_text
            result = local_generation.answer(text, **kwargs)
        except local_generation.LocalGenerationError as exc:
            return ToolResult.failure(
                ToolError(code=exc.kind, message=exc.message),
                usage=_usage(started, None, None),
            )
        except UpstreamError as exc:
            # 检索阶段失败：与上游版保持同一错误类别，但不在 Tool 层重试。
            return ToolResult.failure(
                ToolError(
                    code=ERROR_UPSTREAM,
                    message=exc.message,
                    retryable=(
                        exc.kind == UPSTREAM_TIMEOUT
                        or exc.upstream_status in RETRYABLE_UPSTREAM_STATUS
                    ),
                ),
                usage=_usage(started, None, None),
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            # 只有已知类型的 message 才可用（它们刻意不含提示词/回答正文）；
            # 其它异常一律只留类型名。
            from app.clients.llm import LLMError

            message = exc.message if isinstance(exc, LLMError) else ""
            return ToolResult.failure(
                ToolError(
                    code="local_llm_error",
                    message=message or f"本机生成异常（{type(exc).__name__}）",
                ),
                usage=_usage(started, None, None),
            )

        evidence = local_generation.evidence_from_context(result.context)
        usage = _usage(started, result, evidence)
        return ToolResult.success(
            result.to_payload(include_meta=True), usage=usage, evidence=evidence
        )

    def _invoke_stream(
        self,
        text,
        emit,
        cancelled,
        started: float,
        *,
        history=None,
        memory_text: str = "",
    ) -> ToolResult:
        """把 `iter_stream()` 的事件转发给调用方，结束时仍返回统一 ToolResult。"""
        emitted = {"chunk": False}
        final = None

        def forward(event):
            if str(event.get("type") or "") == "chunk" and event.get("text"):
                emitted["chunk"] = True
            emit(event)

        try:
            kwargs = {}
            if history:
                kwargs["history"] = history
            if memory_text:
                kwargs["memory_text"] = memory_text
            for event in local_generation.iter_stream(
                text, cancelled=cancelled, **kwargs
            ):
                kind = str(event.get("type") or "")
                if kind == "_answer":
                    final = event.get("answer")
                elif kind in ("references", "chunk"):
                    forward(event)
        except local_generation.LocalGenerationError as exc:
            return ToolResult.failure(
                ToolError(
                    code=_stream_code(exc.kind, emitted["chunk"]),
                    message=exc.message,
                ),
                usage=_usage(started, None, None, streamed=emitted["chunk"]),
            )
        except UpstreamError as exc:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_UPSTREAM,
                    message=exc.message,
                    retryable=(
                        exc.kind == UPSTREAM_TIMEOUT
                        or exc.upstream_status in RETRYABLE_UPSTREAM_STATUS
                    ),
                ),
                usage=_usage(started, None, None, streamed=emitted["chunk"]),
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            from app.clients.llm import LLMError

            message = exc.message if isinstance(exc, LLMError) else ""
            return ToolResult.failure(
                ToolError(
                    code="local_llm_error",
                    message=message or f"本机生成异常（{type(exc).__name__}）",
                ),
                usage=_usage(started, None, None, streamed=emitted["chunk"]),
            )

        if final is None:
            return ToolResult.failure(
                ToolError(
                    code=_stream_code("local_unverified", emitted["chunk"]),
                    message="本机流式生成没有返回可用结果",
                ),
                usage=_usage(started, None, None, streamed=emitted["chunk"]),
            )

        evidence = local_generation.evidence_from_context(final.context)
        usage = _usage(started, final, evidence, streamed=emitted["chunk"])
        return ToolResult.success(
            final.to_payload(include_meta=True), usage=usage, evidence=evidence
        )

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        """显式探活本机 Ollama 与目标模型；注册时不会触发。"""
        started = time.perf_counter()
        try:
            probe = local_generation.probe_model()
        except Exception as exc:  # noqa: BLE001 - 探活失败也要返回结构化结果
            from app.clients.llm import LLMError

            message = exc.message if isinstance(exc, LLMError) else ""
            return Health(
                ok=False,
                detail=message or f"本机模型探活异常（{type(exc).__name__}）",
                latency_ms=_elapsed_ms(started),
            )
        detail = f"{probe.get('base_url', '')}，模型 {probe.get('model', '')}"
        if not probe.get("model_present"):
            detail += "（未找到，请先 ollama pull）"
        return Health(
            ok=bool(probe.get("model_present")),
            detail=detail,
            latency_ms=_elapsed_ms(started),
        )


def _validate_query(query: Any) -> Optional[ToolError]:
    if not isinstance(query, str) or len(query.strip()) < 3:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message="query 必须是至少 3 个字符的字符串",
        )
    return None


def _stream_code(kind: str, streamed: bool) -> str:
    """已输出内容后的失败单独标记，方便审计区分“可回退”和“不可回退”。"""
    base = str(kind or "local_generation_error")
    return f"{base}_streamed" if streamed else base


def _usage(
    started: float,
    result: Any,
    evidence: Any,
    *,
    streamed: bool = False,
) -> dict:
    usage: dict = {
        "elapsed_ms": _elapsed_ms(started),
        "generation": "local",
    }
    if streamed:
        usage["streamed"] = True
    if result is None:
        return usage
    timings = getattr(result, "timings_ms", None)
    if isinstance(timings, dict):
        for key in ("retrieve_ms", "build_ms", "llm_ms", "first_token_ms", "total_ms"):
            value = timings.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = round(float(value), 1)
    usage["answer_chars"] = len(str(getattr(result, "response", "") or ""))
    usage["citation_count"] = len(getattr(result, "citations", ()) or ())
    usage["reference_count"] = len(getattr(result, "references", ()) or ())
    usage["evidence_count"] = len(evidence or [])

    # P3C.2：只带布尔/规则码/数字；不带答案、证据或提示词。
    usage["verified"] = bool(getattr(result, "verified", True))
    warnings = getattr(result, "content_warnings", ()) or ()
    codes = [str(code) for code in warnings if isinstance(code, str)]
    usage["content_warning_codes"] = codes[:10]
    usage["content_warning_count"] = len(codes)
    raw_usage = getattr(result, "usage", None)
    if isinstance(raw_usage, dict):
        for key in (
            "prompt_eval_count",
            "eval_count",
            "total_duration_ms",
            "load_duration_ms",
            "prompt_eval_duration_ms",
            "eval_duration_ms",
        ):
            value = raw_usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = value
        done_reason = raw_usage.get("done_reason")
        if isinstance(done_reason, str) and done_reason:
            usage["done_reason"] = done_reason[:20]
    return usage


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


TOOL = LocalAnswerTool()

__all__ = ["LocalAnswerTool", "SPEC", "TOOL"]
