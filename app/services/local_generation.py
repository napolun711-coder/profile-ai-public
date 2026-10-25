"""P3B：本机生成的生产门面（默认上游；只在显式 local 模式生效）。

职责：
- 读冻结配置的 `GENERATION_MODE`；非法/异常一律按 `upstream`；
- 把 P3A 的 `local_chain.answer()` 结果映射成 `/chat` 现有字段：
  `response` / `references` / `response_time`；
- 引用只回传 `chunk / reference / image / document` 四类可见来源，按编号去重，
  图片保留 P2G 需要的 `kind / label / file_id / image_id`；
- 回答必须通过 P3A 的 `classify_answer` 门槛（`ok` 或明确 `refusal`），
  未知编号、无依据又不拒答、空回答都视为失败，由调用方回退原路径；
- 生产 local 模式由本门面构造受控的 `llm_enabled=True` 配置副本，
  `LOCAL_LLM_ENABLED` 仍然只是 P3A 脚本/测试的直调保险丝。

边界：
- 不写文件、不落盘问题/提示词/证据/回答正文；
- 不改变 `SKILL_ROUTING` 的语义；图片问题在技能路由开启时仍由 `image_lookup` 接管；
- 只有显式 `GENERATION_MODE=local` 才会导入并调用本机模型。

P3C：`iter_stream()` 提供真流式；先发 `references`，再逐 token 发 `chunk`，
最后发内部事件 `_answer`。首个 chunk 之前失败仍可让调用方回退上游；
已输出内容后的失败带 `streamed=True`，调用方只报错、不再重复生成。

P3C.2：生成结束后运行确定性内容校验（`validators.content`）：
- error（未知编号/无引用/截断/重复/乱码）按上面的回退策略处理；
- warning（覆盖偏低/可疑数字/拒答等）保留答案，规则码随 `_answer` 返回，
  由流式 `done` 事件暴露，**不记录答案或证据正文**。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

MODE_UPSTREAM = "upstream"
MODE_LOCAL = "local"
GENERATION_MODES = (MODE_UPSTREAM, MODE_LOCAL)

#: 只把这些类型的引用回传给前端；实体/关系仍留在提示词里，但不作为“参考资料”展示。
VISIBLE_KINDS = ("chunk", "reference", "image", "document")


class LocalGenerationError(Exception):
    """本机生成可预期失败（未通过引用门槛等）；调用方据此回退上游。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "local_generation_error",
        streamed: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        #: 已经向调用方输出过 chunk；此时不能再用上游答案覆盖。
        self.streamed = bool(streamed)


@dataclass(frozen=True)
class LocalChatAnswer:
    """一次本机问答的 `/chat` 形状结果；不携带提示词或证据正文。"""

    response: str
    references: Tuple[Dict[str, Any], ...]
    response_time: float
    context: Any
    citations: Tuple[str, ...] = ()
    status: str = "ok"
    timings_ms: Mapping[str, float] = field(default_factory=dict)
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    #: P3C.2：内容校验是否通过（warning 不影响该值）。
    verified: bool = True
    #: P3C.2：warning 规则码；只放规则码，不放答案或编号。
    content_warnings: Tuple[str, ...] = ()

    def to_payload(self, *, include_meta: bool = False) -> Dict[str, Any]:
        """映射成 `/chat` 现有字段，不新增前端契约。

        `include_meta=True` 只给技能工具内部使用（data 里多带 `verified` /
        `warnings`，由 `knowledge_qa` 转进 usage）；`/chat` 直出保持原形状。
        """
        payload = {
            "response": self.response,
            "references": [dict(item) for item in self.references],
            "response_time": self.response_time,
        }
        if include_meta:
            payload["verified"] = bool(self.verified)
            payload["warnings"] = list(self.content_warnings)
        return payload


def generation_mode(settings: Optional[Settings] = None) -> str:
    """读生产回答模式；非法/读取异常一律按 upstream（保证 /chat 可用）。"""
    try:
        cfg = settings if settings is not None else get_settings()
        value = str(getattr(cfg, "generation_mode", "") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 配置坏了也不能拖垮问答
        return MODE_UPSTREAM
    return value if value in GENERATION_MODES else MODE_UPSTREAM


#: P3C.3：进程内正在进行的本机生成数（桌面端 CPU 推理并发会互相拖慢）。
_slots_lock = threading.Lock()
_active_generations = 0


@contextmanager
def _generation_slot(settings: Settings) -> Iterator[None]:
    """占用一个本机生成名額；占不到时抛 `local_busy`，由调用方回退上游。"""
    global _active_generations
    limit = max(1, int(getattr(settings, "llm_max_concurrency", 1) or 1))
    with _slots_lock:
        if _active_generations >= limit:
            raise LocalGenerationError(
                "本机模型正忙，已回退上游", kind="local_busy"
            )
        _active_generations += 1
    try:
        yield
    finally:
        with _slots_lock:
            _active_generations = max(0, _active_generations - 1)


def _context_budget(settings: Settings) -> Any:
    """把配置里的上下文预算映射成 `TokenBudget`；坏值回落到默认 4000。"""
    from app.services.context_builder import TokenBudget

    value = getattr(settings, "llm_context_budget", 4000)
    try:
        total = int(value)
    except (TypeError, ValueError):
        total = 4000
    return TokenBudget(total=max(500, min(32000, total)))


def _content_report(final: Any, *, memory_context: bool = False) -> Any:
    """对一次完整回答运行内容校验；只读内存字段，不记录正文。"""
    from app.services import local_chain
    from app.services.validators.content import validate_content

    context = getattr(final, "context", None)
    citation_map = getattr(context, "citation_map", None)
    evidence_texts: List[str] = []
    groups = getattr(context, "groups", None)
    if isinstance(groups, dict):
        for items in groups.values():
            if not isinstance(items, (list, tuple)):
                continue
            for item in items:
                text = getattr(item, "text", "")
                if isinstance(text, str) and text:
                    evidence_texts.append(text)

    usage = getattr(final, "usage", None)
    done_reason = ""
    if isinstance(usage, dict):
        done_reason = str(usage.get("done_reason") or "")

    answer = str(getattr(final, "answer", "") or "")
    return validate_content(
        answer=answer,
        citations=citation_map if isinstance(citation_map, dict) else {},
        evidence_texts=evidence_texts,
        done_reason=done_reason,
        refusal=local_chain.is_refusal(answer),
        memory_context=memory_context,
    )


def _content_failure(report: Any, *, streamed: bool) -> LocalGenerationError:
    from app.services.validators.content import failure_kind, failure_message

    kind = failure_kind(report)
    # 只记错误类别，不记答案/编号/证据；便于从应用日志定位硬失败原因。
    logger.warning("本机回答内容校验失败：%s", kind)
    return LocalGenerationError(
        failure_message(report),
        kind=kind,
        streamed=streamed,
    )


def _warning_codes(report: Any) -> Tuple[str, ...]:
    from app.services.validators.content import warning_codes

    return tuple(warning_codes(report))


def _citation_key(citation: Any) -> Tuple[str, str]:
    """同一文档的多个 chunk 共享编号，展示层只留一条；图片按 image_id 去重。"""
    reference_id = str(getattr(citation, "reference_id", "") or "").strip()
    if reference_id:
        return ("ref", reference_id)
    image_id = str(getattr(citation, "image_id", "") or "").strip()
    if str(getattr(citation, "kind", "") or "") == "image" and image_id:
        return ("image", image_id)
    return ("citation", str(getattr(citation, "citation_id", "") or ""))


def _visible_citations(built: Any) -> List[Any]:
    citation_map = getattr(built, "citation_map", None)
    if not isinstance(citation_map, dict):
        return []
    seen: set[Tuple[str, str]] = set()
    items: List[Any] = []
    for citation in citation_map.values():
        if str(getattr(citation, "kind", "") or "") not in VISIBLE_KINDS:
            continue
        key = _citation_key(citation)
        if key in seen:
            continue
        seen.add(key)
        items.append(citation)
    return items


def references_from_context(built: Any) -> Tuple[Dict[str, Any], ...]:
    """把 ContextBuilder 的引用映射成前端认识的 `reference_id` / `file_path`。"""
    references: List[Dict[str, Any]] = []
    for citation in _visible_citations(built):
        file_path = str(getattr(citation, "doc_name", "") or "").strip()
        reference_id = str(getattr(citation, "reference_id", "") or "").strip()
        kind = str(getattr(citation, "kind", "") or "")
        file_id = str(getattr(citation, "file_id", "") or "").strip()
        image_id = str(getattr(citation, "image_id", "") or "").strip()
        if not file_path and not reference_id and not (
            kind == "image" and (file_id or image_id)
        ):
            continue

        item: Dict[str, Any] = {
            "reference_id": reference_id,
            "file_path": file_path,
        }
        if kind == "image":
            item["kind"] = "image"
            item["label"] = str(getattr(citation, "label", "") or "")
            if file_id:
                item["file_id"] = file_id
            if image_id:
                item["image_id"] = image_id
        references.append(item)
    return tuple(references)


def evidence_from_context(built: Any) -> List[Any]:
    """把可见引用转换成 `Evidence`，携带 ContextBuilder 的原始 label。

    `knowledge_qa.citations_from_evidence` 会优先使用 `metadata["citation_label"]`，
    因此本机回答里的 `[img-1]` / `[doc-1]` 不会被重新编号。
    """
    from app.plugins.contracts import Evidence

    evidence: List[Any] = []
    for citation in _visible_citations(built):
        metadata: Dict[str, Any] = {"citation_label": str(getattr(citation, "label", "") or "")}
        reference_id = str(getattr(citation, "reference_id", "") or "").strip()
        if reference_id:
            metadata["reference_id"] = reference_id
        evidence.append(
            Evidence(
                evidence_id=str(getattr(citation, "citation_id", "") or ""),
                kind=str(getattr(citation, "kind", "") or ""),
                # 图片/文档没有上游编号时保持空字符串，避免前端把内部 citation_id
                # 当成 reference_id 展示；标签由 metadata["citation_label"] 精确携带。
                source_id=reference_id,
                doc_name=str(getattr(citation, "doc_name", "") or ""),
                file_id=str(getattr(citation, "file_id", "") or ""),
                image_id=str(getattr(citation, "image_id", "") or ""),
                metadata=metadata,
            )
        )
    return evidence


def answer(
    question: str,
    *,
    settings: Optional[Settings] = None,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> LocalChatAnswer:
    """跑一次本机生成；失败抛 `LocalGenerationError` 或上游异常，由调用方回退。"""
    cfg = settings if settings is not None else get_settings()
    if generation_mode(cfg) != MODE_LOCAL:
        raise LocalGenerationError("回答生成方式是上游，未调用本机模型", kind="local_off")

    with _generation_slot(cfg):
        return _answer_locked(
            question, cfg, history=history, memory_text=memory_text
        )


def _answer_locked(
    question: str,
    cfg: Settings,
    *,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> LocalChatAnswer:
    # 延迟导入：upstream 模式不会加载本机客户端与 P3A 链路。
    from app.clients import llm
    from app.services import local_chain

    local_settings = replace(cfg, llm_enabled=True)

    def chat(messages, *, model=None):
        return llm.chat(messages, model=model, settings=local_settings)

    kwargs = {}
    if history:
        kwargs["history"] = history
    if memory_text:
        kwargs["memory_text"] = memory_text
    result = local_chain.answer(
        question,
        chat=chat,
        budget=_context_budget(cfg),
        **kwargs,
    )
    report = _content_report(result, memory_context=bool(memory_text))
    if not report.ok:
        raise _content_failure(report, streamed=False)

    warnings = _warning_codes(report)
    if warnings:
        # 只记规则码，不记答案/证据；便于离线排查 warning 分布。
        logger.info("本机回答内容校验 warning：%s", ",".join(warnings))

    cited_count = int(report.stats.get("cited_count", 0) or 0)
    status = (
        "refusal"
        if cited_count == 0 and local_chain.is_refusal(result.answer)
        else "ok"
    )
    total_ms = float(result.timings_ms.get("total_ms", 0.0) or 0.0)
    return LocalChatAnswer(
        response=result.answer,
        references=references_from_context(result.context),
        response_time=round(total_ms / 1000.0, 3),
        context=result.context,
        citations=tuple(result.cited_labels),
        status=status,
        timings_ms=dict(result.timings_ms),
        model=result.model,
        usage=dict(result.usage),
        verified=True,
        content_warnings=warnings,
    )


def iter_stream(
    question: str,
    *,
    settings: Optional[Settings] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> Iterator[Dict[str, Any]]:
    """流式生成：`references` → `chunk` × N → 内部 `_answer`。

    事件是进程内形状；`/chat/stream` 只转发 `references` / `chunk`。
    生成结束后仍走 `classify_answer`：未知编号或无依据非拒答视为失败，
    已输出过 chunk 时错误带 `streamed=True`，调用方不能再回退上游。
    """
    cfg = settings if settings is not None else get_settings()
    if generation_mode(cfg) != MODE_LOCAL:
        raise LocalGenerationError("回答生成方式是上游，未调用本机模型", kind="local_off")

    with _generation_slot(cfg):
        yield from _iter_stream_locked(
            question,
            cfg,
            cancelled,
            history=history,
            memory_text=memory_text,
        )


def _iter_stream_locked(
    question: str,
    cfg: Settings,
    cancelled: Optional[Callable[[], bool]],
    *,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> Iterator[Dict[str, Any]]:
    from app.clients import llm
    from app.services import local_chain

    local_settings = replace(cfg, llm_enabled=True)

    def chat_stream(messages, *, model=None, cancel=None):
        return llm.stream_chat(
            messages,
            model=model,
            settings=local_settings,
            cancel=cancel,
            first_token_timeout=float(
                max(5, int(getattr(cfg, "llm_first_token_timeout", 120) or 120))
            ),
        )

    built = None
    streamed = False
    final = None

    kwargs = {}
    if history:
        kwargs["history"] = history
    if memory_text:
        kwargs["memory_text"] = memory_text
    for event in local_chain.iter_stream(
        question,
        chat_stream=chat_stream,
        cancelled=cancelled,
        budget=_context_budget(cfg),
        **kwargs,
    ):
        kind = str(event.get("type") or "")
        if kind == "context":
            built = event.get("context")
            continue
        if kind == "chunk":
            text = str(event.get("text") or "")
            if not text:
                continue
            if not streamed and built is not None:
                yield {
                    "type": "references",
                    "items": [dict(item) for item in references_from_context(built)],
                }
            streamed = True
            yield {"type": "chunk", "text": text}
            continue
        if kind == "_result":
            final = event.get("answer")

    if final is None:
        raise LocalGenerationError(
            "本机流式生成没有返回结果",
            kind="local_generation_error",
            streamed=streamed,
        )

    report = _content_report(final, memory_context=bool(memory_text))
    if not report.ok:
        raise _content_failure(report, streamed=streamed)

    cited_count = int(report.stats.get("cited_count", 0) or 0)
    status = (
        "refusal"
        if cited_count == 0 and local_chain.is_refusal(final.answer)
        else "ok"
    )
    warnings = _warning_codes(report)
    if warnings:
        # 只记规则码，不记答案/证据；便于离线排查 warning 分布。
        logger.info("本机流式回答内容校验 warning：%s", ",".join(warnings))
    total_ms = float(final.timings_ms.get("total_ms", 0.0) or 0.0)
    yield {
        "type": "_answer",
        "answer": LocalChatAnswer(
            response=final.answer,
            references=references_from_context(final.context),
            response_time=round(total_ms / 1000.0, 3),
            context=final.context,
            citations=tuple(final.cited_labels),
            status=status,
            timings_ms=dict(final.timings_ms),
            model=final.model,
            usage=dict(final.usage),
            verified=True,
            content_warnings=warnings,
        ),
    }


def probe_model(settings: Optional[Settings] = None, *, model: Optional[str] = None) -> Dict[str, Any]:
    """探活本机 Ollama 与目标模型；不生成、不改状态。

    生产 local 模式显式构造 `llm_enabled=True` 副本，因此不需要打开
    `LOCAL_LLM_ENABLED`；非 local 模式不会调用这里。
    """
    from app.clients import llm

    cfg = settings if settings is not None else get_settings()
    return llm.probe(settings=replace(cfg, llm_enabled=True), model=model)


__all__ = [
    "GENERATION_MODES",
    "LocalChatAnswer",
    "LocalGenerationError",
    "MODE_LOCAL",
    "MODE_UPSTREAM",
    "VISIBLE_KINDS",
    "answer",
    "evidence_from_context",
    "generation_mode",
    "iter_stream",
    "probe_model",
    "references_from_context",
]
