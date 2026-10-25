"""P3A：本地生成链路（检索 → ContextBuilder → Prompt → 本地 LLM）。

组成：
    1. `lightrag.retrieve()` 调 `/query/data`（只检索，不生成）；
    2. P0 适配器把结果归一化成 `Evidence`；
    3. P1C `build_context()` 组装 context 与 citation_map；
    4. P3A `prompts` 生成 L0–L3 system prompt + L4 user 问题；
    5. `app.clients.llm.chat()` 调本机 Ollama（只允许回环地址）。

边界：
- P3A 时只被验收脚本与测试导入；P3B 起生产门面（`app/services/local_generation.py`）
  也会调用，但本模块自身仍然不读全局开关、不发请求（默认调用方由生产门面注入）；
- 不写文件、不落盘提示词/证据正文；耗时与用量只存在内存返回值里；
- `retrieve` / `chat` 都可注入，单元测试不联网；
- 引用核对只做“回答里的标签是否存在于 citation_map”，**不判断是否编造**（P3C.2）。

P3C：`iter_stream()` 在检索/组装完成后逐 token 产出；首 token 与总耗时写进
`timings_ms["first_token_ms"]`。非流式 `answer()` 保持原样。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from app.clients import lightrag, llm
from app.plugins.adapters.knowledge import evidence_from_query_data
from app.services import prompts
from app.services.context_builder import BuiltContext, Citation, RuleSet, TokenBudget, build_context
from app.services.validators.content import REFUSAL_MARKERS as REFUSAL_MARKERS
from app.services.validators.content import is_refusal

#: P3A 只识别 ContextBuilder 会产生的那几类标签，避免把普通方括号当成引用。
CITATION_RE = re.compile(
    r"\[(?:\d+|img-\d+|doc-\d+|"
    r"(?:chunk|entity|relationship|reference|document):[^\[\]\r\n]{1,120})\]"
)

#: 明确说明“资料不足”的回答不需要引用；这类拒答是 L1 要求的行为。
#: 判定（含 `REFUSAL_MARKERS`）与 P3C.2 内容校验共用，避免两套标准。


class LocalChainError(Exception):
    """链路可预期失败（无证据 / 空回答等）；`kind` 供脚本汇总。"""

    def __init__(self, message: str, *, kind: str = "local_chain_error") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass(frozen=True)
class LocalAnswer:
    question: str
    answer: str
    context: BuiltContext
    messages: List[Dict[str, str]]
    cited_labels: List[str]
    unknown_labels: List[str]
    timings_ms: Dict[str, float]
    model: str
    usage: Dict[str, Any] = field(default_factory=dict)


def extract_citations(text: str) -> List[str]:
    """按首次出现顺序返回去重后的引用标签（保留方括号）。"""
    seen: set[str] = set()
    labels: List[str] = []
    for match in CITATION_RE.finditer(str(text or "")):
        label = match.group(0)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def unknown_citations(
    labels: Sequence[str],
    citation_map: Mapping[str, Citation],
) -> List[str]:
    """返回回答里出现、但不在 citation_map 中的标签。"""
    known = {citation.label for citation in citation_map.values()}
    return [label for label in labels if label not in known]


def classify_answer(
    answer: str,
    cited: Sequence[str],
    unknown: Sequence[str],
) -> str:
    """把一次回答分成 ok / refusal / fail。

    - unknown 非空：回答引用了资料中不存在的编号 → fail；
    - 有引用：ok；
    - 无引用但明确说明资料不足：refusal（L1 要求的正常行为）；
    - 其余：fail（既没有依据，也没有明确拒答）。
    """
    if unknown:
        return "fail"
    if cited:
        return "ok"
    if is_refusal(str(answer or "")):
        return "refusal"
    return "fail"


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def answer(
    question: str,
    *,
    retrieve: Optional[Callable[..., Dict[str, Any]]] = None,
    chat: Optional[Callable[..., llm.LLMReply]] = None,
    budget: Optional[TokenBudget] = None,
    output_format: str = "",
    model: Optional[str] = None,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> LocalAnswer:
    """跑通一次本地链路；失败抛 `LocalChainError` 或上游异常。"""
    text = str(question or "").strip()
    if not text:
        raise ValueError("question 不能为空")

    retrieve_fn = retrieve or lightrag.retrieve
    chat_fn = chat or llm.chat

    started = time.perf_counter()
    payload = retrieve_fn(text)
    retrieve_ms = _elapsed_ms(started)

    built = build_context(
        question=text,
        evidence=evidence_from_query_data(payload),
        rules=RuleSet(system_prompt=prompts.build_system_prompt()),
        output_format=output_format,
        budget=budget,
    )
    build_ms = _elapsed_ms(started) - retrieve_ms

    # 没有证据时仍然把“无资料”告诉模型，让它按 L1 明确拒绝，而不是凭空回答。
    prompt_kwargs = {}
    if history:
        prompt_kwargs["history"] = history
    if memory_text:
        prompt_kwargs["prefix_text"] = memory_text
    messages = prompts.build_messages(
        question=text,
        context_text=built.context_text,
        system_prompt=built.system_prompt,
        **prompt_kwargs,
    )

    llm_started = time.perf_counter()
    reply = chat_fn(messages, model=model)
    llm_ms = _elapsed_ms(llm_started)

    content = str(getattr(reply, "content", "") or "").strip()
    if not content:
        raise LocalChainError("本地模型返回空回答", kind="empty_answer")

    cited = extract_citations(content)
    unknown = unknown_citations(cited, built.citation_map)
    total_ms = _elapsed_ms(started)

    return LocalAnswer(
        question=text,
        answer=content,
        context=built,
        messages=messages,
        cited_labels=cited,
        unknown_labels=unknown,
        timings_ms={
            "retrieve_ms": retrieve_ms,
            "build_ms": round(build_ms, 1),
            "llm_ms": llm_ms,
            "total_ms": total_ms,
        },
        model=str(getattr(reply, "model", "") or model or ""),
        usage=dict(getattr(reply, "usage", {}) or {}),
    )


def iter_stream(
    question: str,
    *,
    retrieve: Optional[Callable[..., Dict[str, Any]]] = None,
    chat_stream: Optional[Callable[..., Any]] = None,
    budget: Optional[TokenBudget] = None,
    output_format: str = "",
    model: Optional[str] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    history: Optional[Sequence[Mapping[str, object]]] = None,
    memory_text: str = "",
) -> Iterator[Dict[str, Any]]:
    """流式版链路：先发 context，再逐 token 发 chunk，最后发 `_result`。

    事件形状（只在进程内使用，不直接回给前端）：
    - `{"type": "context", "context": BuiltContext}`：引用映射已就绪；
    - `{"type": "chunk", "text": str}`：模型增量；
    - `{"type": "_result", "answer": LocalAnswer}`：完整结果与耗时。

    取消、LLM 错误与空回答都会向上抛；调用方根据是否已收到 chunk 决定回退策略。
    """
    text = str(question or "").strip()
    if not text:
        raise ValueError("question 不能为空")

    retrieve_fn = retrieve or lightrag.retrieve
    stream_fn = chat_stream or llm.stream_chat

    started = time.perf_counter()
    payload = retrieve_fn(text)
    retrieve_ms = _elapsed_ms(started)

    built = build_context(
        question=text,
        evidence=evidence_from_query_data(payload),
        rules=RuleSet(system_prompt=prompts.build_system_prompt()),
        output_format=output_format,
        budget=budget,
    )
    build_ms = _elapsed_ms(started) - retrieve_ms
    yield {"type": "context", "context": built}

    prompt_kwargs = {}
    if history:
        prompt_kwargs["history"] = history
    if memory_text:
        prompt_kwargs["prefix_text"] = memory_text
    messages = prompts.build_messages(
        question=text,
        context_text=built.context_text,
        system_prompt=built.system_prompt,
        **prompt_kwargs,
    )

    llm_started = time.perf_counter()
    first_token_ms = 0.0
    parts: List[str] = []
    reply_model = str(model or "")
    usage: Dict[str, Any] = {}

    for chunk in stream_fn(messages, model=model, cancel=cancelled):
        content = str(getattr(chunk, "content", "") or "")
        if content:
            if not first_token_ms:
                first_token_ms = _elapsed_ms(llm_started)
            parts.append(content)
            yield {"type": "chunk", "text": content}
        chunk_model = str(getattr(chunk, "model", "") or "")
        if chunk_model:
            reply_model = chunk_model
        chunk_usage = getattr(chunk, "usage", None)
        if isinstance(chunk_usage, dict) and chunk_usage:
            usage = dict(chunk_usage)

    llm_ms = _elapsed_ms(llm_started)
    content = "".join(parts).strip()
    if not content:
        raise LocalChainError("本地模型返回空回答", kind="empty_answer")

    cited = extract_citations(content)
    unknown = unknown_citations(cited, built.citation_map)
    total_ms = _elapsed_ms(started)

    yield {
        "type": "_result",
        "answer": LocalAnswer(
            question=text,
            answer=content,
            context=built,
            messages=messages,
            cited_labels=cited,
            unknown_labels=unknown,
            timings_ms={
                "retrieve_ms": retrieve_ms,
                "build_ms": round(build_ms, 1),
                "llm_ms": llm_ms,
                "first_token_ms": round(first_token_ms, 1),
                "total_ms": total_ms,
            },
            model=reply_model,
            usage=usage,
        ),
    }
