"""POST /chat：知识库问答（Phase 2 从根目录 main.py 原样迁移）。

请求体：{"message": "..."}
返回：默认仍是 LightRAG /query 的原始 JSON（直透上游）；
      P2F 打开 `SKILL_ROUTING=on` 时才走技能路由，失败自动回退上游；
      P3B 打开 `GENERATION_MODE=local` 时用本机模型生成，失败同样回退。

边界：
- 两个开关默认都关闭，关闭路径不导入技能运行时/本机模型、不建 ToolManager；
- 技能路径只映射 response / references / response_time，不改前端事件形状；
- 技能异常、空答案、图片技能没有证据、本机生成失败时回退
  `lightrag.query()` / `query_stream()`；
- P3C：`GENERATION_MODE=local` 时 `/chat/stream` 逐 token 输出；
  首个 chunk 之前失败仍回退上游，已输出内容后失败只发 `error` 并结束，
  不再重新生成上游（避免重复内容）。
- P3F：请求可带一次性的 `force_upstream=true`（只用于前端手动重试），
  本次跳过本机与技能；本机已输出后失败的 `error` 带 `retry_upstream=true`，
  前端据此显示按钮，**不会自动重发**。
"""

import json
import logging

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from app.clients import lightrag
from app.core.config import get_settings
from app.core.errors import UpstreamError
from app.memory import chat_bridge
from app.memory.context import ChatContext
from app.models.schemas import Question
from app.services.context_builder import estimate_tokens

logger = logging.getLogger(__name__)

router = APIRouter()

def _skill_routing_on() -> bool:
    """读启动时冻结的配置；读取失败按关闭处理，保证 /chat 可用。"""
    try:
        return str(get_settings().skill_routing or "").strip().lower() == "on"
    except Exception:  # noqa: BLE001 - 配置异常不能拖垮问答
        return False


def _generation_mode() -> str:
    """读启动时冻结的配置；非法/异常按 upstream，保证 /chat 可用。"""
    try:
        value = str(get_settings().generation_mode or "").strip().lower()
    except Exception:  # noqa: BLE001 - 配置异常不能拖垮问答
        return "upstream"
    return value if value in ("upstream", "local") else "upstream"


def _upstream_context_kwargs(context: ChatContext = None) -> dict:
    """只在有内容、上游支持、且对应外发开关允许时才发送新字段。

    阶段 0：历史与长期记忆分开控制（MEMORY_UPSTREAM_HISTORY /
    MEMORY_UPSTREAM_MEMORY）。默认 off —— 只打开记忆上下文但没打开外发开关时，
    请求体保持改造前形状。
    """
    if context is None:
        return {}
    supported = lightrag.supports_query_context()
    context.trace["upstream_context_supported"] = bool(supported)
    if not supported:
        context.trace["upstream_context_delivery"] = "unsupported"
        return {}
    if not context.has_upstream_context:
        context.trace["upstream_context_delivery"] = "empty"
        return {}
    settings = get_settings()
    history_mode = str(getattr(settings, "memory_upstream_history", "off") or "off").lower()
    memory_mode = str(getattr(settings, "memory_upstream_memory", "off") or "off").lower()
    kwargs = {}
    if context.history and history_mode != "off":
        kwargs["conversation_history"] = context.history
    # 会话状态属于“历史连续性”通道：历史开关打开就发送；长期记忆仍单独受
    # MEMORY_UPSTREAM_MEMORY 控制，避免跨会话记忆借会话状态偷偷外发。
    prompt_parts = []
    if context.session_prompt and history_mode != "off":
        prompt_parts.append(context.session_prompt)
    if memory_mode != "off":
        if context.memory_prompt:
            prompt_parts.append(context.memory_prompt)
        if context.recall_prompt:
            prompt_parts.append(context.recall_prompt)
    if prompt_parts:
        kwargs["user_prompt"] = "\n\n".join(prompt_parts)
    # 只记“实际外发结构”，不记正文；供验收确认历史/会话状态没有在请求前丢失。
    context.trace["upstream_context_delivery"] = "sent" if kwargs else "empty"
    context.trace["upstream_context_fields"] = sorted(kwargs)
    context.trace["upstream_history_count"] = len(
        kwargs.get("conversation_history") or []
    )
    context.trace["upstream_user_prompt_tokens"] = estimate_tokens(
        kwargs.get("user_prompt") or ""
    )
    return kwargs


def _skill_options(context: ChatContext = None):
    if context is None:
        return None
    if not context.has_upstream_context and not context.topic_id:
        return None
    return {
        "conversation_history": context.history,
        "user_prompt": context.user_prompt,
        "memory_text": context.local_prefix,
        "topic_id": context.topic_id,
    }


def _local_answer(
    message: str,
    context: ChatContext = None,
):
    """通用技能路由关闭且模式为 local 时，先试本机生成。"""
    if _generation_mode() != "local" or _skill_routing_on():
        return None

    try:
        # 延迟导入：upstream / 技能路由路径完全不加载本机生成门面。
        from app.services import local_generation

        if context is not None and (context.history or context.local_prefix):
            return local_generation.answer(
                message,
                history=context.history,
                memory_text=context.local_prefix,
            )
        return local_generation.answer(message)
    except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
        logger.warning("本机生成不可用，回退上游：%s", type(exc).__name__)
        return None


def _skill_answer(message: str, context: ChatContext = None):
    """尝试技能路径；任何不可用都返回 None，由调用方回退上游。"""
    if not _skill_routing_on():
        return None

    try:
        # 延迟导入：off 路径完全不加载 builtin 技能与工具适配器。
        from app.skills.production import get_runtime

        runtime = get_runtime()
        if runtime is None:
            return None
        options = _skill_options(context)
        return runtime.answer(message, options=options) if options else runtime.answer(message)
    except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
        logger.warning("技能路由不可用，回退上游：%s", type(exc).__name__)
        return None


def _source_label(
    force_upstream: bool = False,
    force_bypass: bool = False,
) -> str:
    """M1 只做尽力标注；实际路径发生回退时仍以事件/响应为准。"""
    if force_upstream:
        return "upstream_retry"
    if force_bypass:
        return "upstream_bypass"
    if _generation_mode() == "local":
        return "local"
    if _skill_routing_on():
        return "skill"
    return "upstream"


@router.post("/chat")
def chat(data: Question, response: Response = None):
    """非流式问答；M1 在 MEMORY_ENABLED=on 时把本轮写入会话库。

    持久化失败不影响回答；``X-Session-Id`` 只在成功准备持久化时返回。
    """
    force_bypass = bool(getattr(data, "force_bypass", False))
    force_upstream = bool(getattr(data, "force_upstream", False))
    source = _source_label(force_upstream, force_bypass)
    persistence = chat_bridge.begin_chat(data, source=source)
    context, context_status = chat_bridge.build_context(persistence, data.message)
    if response is not None:
        if persistence is not None:
            response.headers["X-Session-Id"] = persistence.session_id
        response.headers["X-Context-Status"] = context_status.get("status", "")
        response.headers["X-Context-Reason"] = context_status.get("reason", "")

    try:
        # 阶段 4 MVP：用户手动选择“不查资料继续”，只走 bypass + 会话上下文。
        if force_bypass:
            result = lightrag.query(
                _bypass_query(data.message, context),
                mode="bypass",
                **_upstream_context_kwargs(context),
            )
        # P3F：前端手动“换上游重试”只影响本次请求，跳过本机与技能路径。
        elif getattr(data, "force_upstream", False):
            result = lightrag.query(
                data.message, **_upstream_context_kwargs(context)
            )
        else:
            answer = (
                _local_answer(data.message, context)
                if context is not None
                else _local_answer(data.message)
            )
            if answer is not None:
                result = answer.to_payload()
            else:
                answer = (
                    _skill_answer(data.message, context)
                    if context is not None
                    else _skill_answer(data.message)
                )
                result = (
                    answer.to_payload()
                    if answer is not None
                    else lightrag.query(
                        data.message, **_upstream_context_kwargs(context)
                    )
                )
    except Exception as exc:  # noqa: BLE001 - 先标记落盘状态，再按原逻辑抛出
        chat_bridge.fail(
            persistence, exc, source=source, context_status=context_status
        )
        raise

    chat_bridge.finish_from_payload(
        persistence,
        result,
        source=source,
        trace=context.trace if context is not None else None,
        context_status=context_status,
        bypass=force_bypass,
    )
    return result


def _line(payload: dict) -> str:
    # NDJSON：每行一个 JSON 对象（前端按行增量解析）
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _bypass_query(message: str, context: ChatContext = None) -> str:
    """bypass 模式不读 user_prompt；把会话状态并入 query，保证约束/摘要不丢。"""
    if context is None or not str(context.session_prompt or "").strip():
        return message
    return (
        str(context.session_prompt).strip()
        + "\n\n## 本轮问题\n"
        + str(message or "").strip()
    )


def _upstream_events(
    message: str, context: ChatContext = None, *, mode: str = ""
):
    """把 LightRAG 的 NDJSON 流规整为前端事件；与改造前逐项一致。"""
    kwargs = _upstream_context_kwargs(context)
    if mode:
        kwargs["mode"] = mode
    try:
        for event in lightrag.query_stream(message, **kwargs):
            # 注意：LightRAG 可能把 response 与 references 放在同一行，故逐项独立判断
            if "references" in event:
                yield {"type": "references", "items": event.get("references") or []}
            if "response" in event:
                piece = event.get("response") or ""
                if piece:
                    yield {"type": "chunk", "text": piece}
            if "error" in event:
                yield {"type": "error", "message": str(event.get("error"))}
            if "response_time" in event:
                yield {"type": "done", "response_time": event.get("response_time")}
    except UpstreamError as exc:
        yield {"type": "error", "message": exc.message}
    yield {"type": "end"}


#: 已经输出过内容后的本机失败文案；此时不再回退上游，避免同一问题生成两遍。
LOCAL_STREAM_ERROR = "本机生成中断，已保留已输出内容（未回退上游）"

#: P3C.2：`done` 事件只带这些用量字段（数字 + done_reason），不带正文。
_USAGE_KEYS = (
    "prompt_eval_count",
    "eval_count",
    "total_duration_ms",
    "load_duration_ms",
    "prompt_eval_duration_ms",
    "eval_duration_ms",
)


def _safe_usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    usage = {}
    for key in _USAGE_KEYS:
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            usage[key] = item
    done_reason = value.get("done_reason")
    if isinstance(done_reason, str) and done_reason:
        usage["done_reason"] = done_reason[:20]
    return usage


def _stream_error_message(exc: Exception) -> str:
    """已输出后的错误文案：优先用本机生成的安全原因，再补“未回退”说明。"""
    try:
        from app.services.local_generation import LocalGenerationError

        if isinstance(exc, LocalGenerationError):
            message = str(exc.message or "").strip()
            if message:
                return f"{message}（已保留已输出内容，未回退上游）"
    except Exception:  # noqa: BLE001 - 延迟导入失败时用通用文案
        pass
    return LOCAL_STREAM_ERROR


def _done_event(answer, *, include_content_meta: bool = False) -> dict:
    """本机 `done`：保留 response_time，另带可选的校验/用量元数据。"""
    event = {
        "type": "done",
        "response_time": getattr(answer, "response_time", 0.0),
    }
    if not include_content_meta:
        return event
    verified = getattr(answer, "verified", None)
    if isinstance(verified, bool):
        event["verified"] = verified
    warnings = getattr(answer, "content_warnings", None)
    if warnings is None:
        warnings = getattr(answer, "warnings", ())
    if not isinstance(warnings, (list, tuple)):
        warnings = ()
    event["warnings"] = [str(code) for code in warnings if isinstance(code, str)][:10]
    usage = _safe_usage(getattr(answer, "usage", None))
    if usage:
        event["usage"] = usage
    return event


def _direct_local_stream_events(message: str, context: ChatContext = None):
    """技能路由关闭 + local：直接消费本机流式门面。"""
    emitted = False
    answer = None
    try:
        from app.services import local_generation

        stream_kwargs = {}
        if context is not None and (context.history or context.local_prefix):
            stream_kwargs = {
                "history": context.history,
                "memory_text": context.local_prefix,
            }
        for event in local_generation.iter_stream(message, **stream_kwargs):
            kind = str(event.get("type") or "")
            if kind == "_answer":
                answer = event.get("answer")
            elif kind in ("references", "chunk"):
                if kind == "chunk":
                    emitted = True
                yield event
    except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
        # 只记类型与规则码，不记问题/答案/提示词。
        kind = str(getattr(exc, "kind", "") or "")
        logger.warning(
            "本机流式生成失败：%s%s",
            type(exc).__name__,
            f"（{kind}）" if kind else "",
        )
        if emitted:
            yield {
                "type": "error",
                "message": _stream_error_message(exc),
                # P3F：只标记“可以手动换上游重试”，前端不会自动重发。
                "retry_upstream": True,
            }
            yield {"type": "end"}
            return
        # 回退上游仍必须保留已经装配好的会话上下文，不能静默退化为普通请求。
        yield from _upstream_events(message, context)
        return

    if answer is not None:
        yield _done_event(answer, include_content_meta=True)
        yield {"type": "end"}
        return
    yield from _upstream_events(message, context)


def _skill_stream_events(message: str, context: ChatContext = None):
    """技能路由开启 + local：通过 manager/tool 的 emit 回调流式输出。"""
    try:
        # 延迟导入：upstream 路径完全不加载技能运行时。
        from app.skills.production import get_runtime

        runtime = get_runtime()
    except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
        logger.warning("技能路由不可用，回退上游：%s", type(exc).__name__)
        yield from _upstream_events(message, context)
        return

    if runtime is None:
        yield from _upstream_events(message, context)
        return

    emitted = False
    answer = None
    error_message = ""
    try:
        options = _skill_options(context)
        stream = (
            runtime.stream_answer(message, options=options)
            if options
            else runtime.stream_answer(message)
        )
        for event in stream:
            kind = str(event.get("type") or "")
            if kind == "_answer":
                answer = event.get("answer")
                error_message = str(event.get("error") or "")
            elif kind in ("references", "chunk"):
                if kind == "chunk":
                    emitted = True
                yield event
    except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
        kind = str(getattr(exc, "kind", "") or "")
        logger.warning(
            "技能流式生成失败：%s%s",
            type(exc).__name__,
            f"（{kind}）" if kind else "",
        )
        if emitted:
            yield {
                "type": "error",
                "message": LOCAL_STREAM_ERROR,
                "retry_upstream": True,
            }
            yield {"type": "end"}
            return
        yield from _upstream_events(message, context)
        return

    if answer is not None:
        if not emitted:
            # 图片技能等整段返回的技能没有 emit；先补 references 再发整段答案。
            if answer.references:
                yield {
                    "type": "references",
                    "items": [dict(item) for item in answer.references],
                }
            if answer.response:
                yield {"type": "chunk", "text": answer.response}
        yield _done_event(answer, include_content_meta=True)
        yield {"type": "end"}
        return

    if emitted:
        yield {
            "type": "error",
            "message": error_message or LOCAL_STREAM_ERROR,
            "retry_upstream": True,
        }
        yield {"type": "end"}
        return
    yield from _upstream_events(message, context)


def _stream_events(
    message: str,
    *,
    force_upstream: bool = False,
    force_bypass: bool = False,
    context: ChatContext = None,
):
    """local 模式走真流式；否则保持 P2F 的整段技能/上游行为。

    P3F：`force_upstream=True` 只服务前端的手动“换上游重试”，
    跳过本机与技能路径，不改变默认路由。
    阶段 4 MVP：`force_bypass=True` 只服务“不查资料继续”，走 bypass。
    """
    if force_bypass:
        yield from _upstream_events(
            _bypass_query(message, context), context, mode="bypass"
        )
        return
    if force_upstream:
        yield from _upstream_events(message, context)
        return
    if _generation_mode() == "local":
        if _skill_routing_on():
            yield from _skill_stream_events(message, context)
        else:
            yield from _direct_local_stream_events(message, context)
        return

    answer = _skill_answer(message, context) if _skill_routing_on() else None
    if answer is not None:
        yield {"type": "references", "items": [dict(item) for item in answer.references]}
        if answer.response:
            # 上游模式保持 P2F 行为：整段答案一个 chunk，不做假 token 分片。
            yield {"type": "chunk", "text": answer.response}
        yield {"type": "done", "response_time": answer.response_time}
        yield {"type": "end"}
        return

    yield from _upstream_events(message, context)


@router.post("/chat/stream")
def chat_stream(data: Question):
    # 流式问答：技能可用时输出整段答案；否则把上游 NDJSON 规整后转发。
    # M1：开始前同步写用户消息与助手占位；生成器关闭时落盘 partial。
    force_bypass = bool(getattr(data, "force_bypass", False))
    force_upstream = bool(getattr(data, "force_upstream", False))
    source = _source_label(force_upstream, force_bypass)
    persistence = chat_bridge.begin_chat(data, source=source)
    context, context_status = chat_bridge.build_context(persistence, data.message)

    def generate():
        # 上下文装配失败不能静默：先发一个状态事件，前端据此显示非阻断提示。
        if context_status.get("status") == "failed":
            yield {
                "type": "context",
                "status": "failed",
                "reason": str(context_status.get("reason") or ""),
            }
        events = (
            _stream_events(
                data.message,
                force_upstream=force_upstream,
                force_bypass=force_bypass,
                context=context,
            )
            if context is not None
            else _stream_events(
                data.message,
                force_upstream=force_upstream,
                force_bypass=force_bypass,
            )
        )
        yield from chat_bridge.wrap_stream(
            events,
            persistence,
            source=source,
            trace=context.trace if context is not None else None,
            context_status=context_status,
            bypass=force_bypass,
        )
        # 注意：wrap_stream 原样产出事件；这里只负责 NDJSON 序列化。

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Context-Status": str(context_status.get("status") or ""),
        "X-Context-Reason": str(context_status.get("reason") or ""),
    }
    if persistence is not None:
        headers["X-Session-Id"] = persistence.session_id

    def lines():
        for event in generate():
            yield _line(event)

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers=headers,
    )
