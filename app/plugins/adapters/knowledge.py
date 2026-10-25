"""P0：知识库检索工具适配器（只读，包装 lightrag.retrieve → POST /query/data）。

关键边界：
- 只检索、不生成答案；不碰 /query、/query/stream；
- `/query/data` 的原始返回放在 `ToolResult.data`，不做裁剪（P0 先证明契约）；
- 同时把 entities / relationships / chunks / references 归一化成 `Evidence`，
  供 P1 的 Context Builder 使用；`Evidence.text` 只留在内存里，P0 的影子脚本不落正文。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.clients import lightrag
from app.core.config import get_settings
from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError
from app.memory.store import document_key
from app.plugins.contracts import (
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_UPSTREAM,
    RETRYABLE_UPSTREAM_STATUS,
    Evidence,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)

ERROR_TOPIC_UNAVAILABLE = "topic_filter_unavailable"
ERROR_TOPIC_NO_DOCUMENTS = "topic_no_documents"
ERROR_TOPIC_NO_EVIDENCE = "topic_no_evidence"
#: bypass 生成时放进 prompt 的资料上限；超出按 chunk 顺序截断。
MAX_TOPIC_CONTEXT_CHARS = 12000

SPEC = ToolSpec(
    name="knowledge.search",
    version="0.2.0",
    description="只读检索知识库（LightRAG /query/data），返回实体、关系、片段与引用",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题（至少 3 个字符）"},
            "top_k": {"type": "integer", "description": "实体/关系检索条数"},
            "chunk_top_k": {"type": "integer", "description": "文本片段检索条数"},
            "mode": {
                "type": "string",
                "enum": list(lightrag.QUERY_MODES),
                "description": "检索模式；缺省 mix",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "message": {"type": "string"},
            "data": {
                "type": "object",
                "properties": {
                    "entities": {"type": "array"},
                    "relationships": {"type": "array"},
                    "chunks": {"type": "array"},
                    "references": {"type": "array"},
                },
            },
            "metadata": {"type": "object"},
        },
    },
    capabilities=("retrieval", "knowledge_search", "read_only"),
    side_effect="none",
    external=True,       # LightRAG 可能按它自己的配置调用外部 LLM / Embedding / Rerank
    risk="low",
    timeout_s=180.0,
    # P1A：只读检索可以安全重试一次；写操作永远不会走到这条策略。
    retry=RetryPolicy(
        max_attempts=2,
        backoff_s=0.5,
        retry_on=("upstream_error",),
    ),
    allowed_for_skills=True,
)


# P2B：只读问答工具。刻意**不加入 BUILTIN_TOOLS**：
# - `/api/tools` 与现有设置页保持原样；
# - 只有影子脚本/测试显式注册它，避免在 P2B 就改变生产可见面。
ANSWER_SPEC = ToolSpec(
    name="knowledge.answer",
    version="0.1.0",
    description="只读回答知识库问题（LightRAG /query，强制附带 references）",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "问题（至少 3 个字符）"},
            "conversation_history": {
                "type": "array",
                "description": "近期对话（只进入 LLM，不参与检索）",
            },
            "user_prompt": {
                "type": "string",
                "description": "长期记忆/使用规则（Additional Instructions）",
            },
            "topic_id": {
                "type": "string",
                "description": "资料集 ID；CHAT_TOPIC_FILTER=on 时只使用本案文档",
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
    capabilities=("answer", "knowledge_qa", "read_only"),
    side_effect="none",
    external=True,
    risk="low",
    timeout_s=240.0,
    # 问答会调用上游 LLM；失败重试可能重复计费/重复生成，交给 client 的
    # 连接/5xx 重试即可，Tool 层不额外重试。
    retry=RetryPolicy(),
    allowed_for_skills=True,
)


class KnowledgeSearchTool:
    """把 `lightrag.retrieve()` 收敛成统一 Tool 返回；失败不抛异常。"""

    spec = SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        query: Any,
        top_k: Optional[int] = None,
        chunk_top_k: Optional[int] = None,
        mode: Optional[str] = None,
    ) -> ToolResult:
        started = time.perf_counter()
        usage = _usage(started, mode, top_k, chunk_top_k)

        invalid = _validate(query, top_k, chunk_top_k, mode)
        if invalid is not None:
            return ToolResult.failure(invalid, usage=usage)

        try:
            payload = lightrag.retrieve(
                query,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                mode=mode or lightrag.QUERY_MODE,
            )
        except UpstreamError as exc:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_UPSTREAM,
                    message=exc.message,
                    retryable=_retryable(exc),
                    detail=exc.upstream_body or "",
                    upstream_status=exc.upstream_status,
                ),
                usage=usage,
            )
        except ValueError as exc:
            # retrieve() 自己也会做一次防御性校验；映射成同一类输入错误。
            return ToolResult.failure(
                ToolError(code=ERROR_INVALID_INPUT, message=str(exc)),
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INTERNAL,
                    message=f"检索工具异常（{type(exc).__name__}）：{exc}",
                ),
                usage=usage,
            )

        return ToolResult.success(
            payload,
            usage=_usage(started, mode, top_k, chunk_top_k),
            evidence=evidence_from_query_data(payload),
        )

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        """显式探活（GET /health）；导入、注册、创建工具时都不会触发。"""
        started = time.perf_counter()
        try:
            probe = lightrag.probe_health()
        except Exception as exc:  # noqa: BLE001 - 探活失败也要返回结构化结果
            return Health(
                ok=False,
                detail=f"探活异常（{type(exc).__name__}）：{exc}",
                latency_ms=_elapsed_ms(started),
            )
        if not isinstance(probe, dict):
            return Health(ok=False, detail="探活返回了非对象结果", latency_ms=_elapsed_ms(started))
        status = probe.get("status_code")
        detail = str(probe.get("detail") or (f"HTTP {status}" if status else ""))
        return Health(
            ok=bool(probe.get("ok")),
            detail=detail,
            latency_ms=_elapsed_ms(started),
        )


class KnowledgeAnswerTool:
    """把 `lightrag.query(include_references=True)` 收敛成统一 Tool 返回。

    与 `knowledge.search` 的分工：
    - `knowledge.search` 只检索（/query/data），不生成；
    - `knowledge.answer` 生成答案并强制带上 references，供 `knowledge_qa` 显式化引用。

    边界：不修改 `/chat` 的调用路径；不缓存；不把上游正文写进 trace/审计。
    """

    spec = ANSWER_SPEC

    def invoke(
        self,
        ctx: Optional[ToolContext] = None,
        *,
        query: Any,
        conversation_history: Any = None,
        user_prompt: Any = None,
        topic_id: Any = None,
    ) -> ToolResult:
        started = time.perf_counter()
        invalid = _validate_answer_query(query)
        if invalid is not None:
            return ToolResult.failure(invalid, usage=_answer_usage(started, None))

        text = query.strip()
        topic = str(topic_id or "").strip()
        if topic and get_settings().chat_topic_filter:
            return _answer_with_topic(
                text,
                topic,
                conversation_history=conversation_history,
                user_prompt=user_prompt,
                started=started,
            )

        kwargs = {}
        history = (
            conversation_history
            if isinstance(conversation_history, (list, tuple))
            else None
        )
        prompt = user_prompt if isinstance(user_prompt, str) else ""
        if (history or prompt.strip()) and lightrag.supports_query_context():
            if history:
                kwargs["conversation_history"] = list(history)
            if prompt.strip():
                kwargs["user_prompt"] = prompt
        try:
            payload = lightrag.query(
                text, include_references=True, **kwargs
            )
        except UpstreamError as exc:
            return ToolResult.failure(
                ToolError(
                    code=ERROR_UPSTREAM,
                    message=exc.message,
                    retryable=_retryable(exc),
                    detail=exc.upstream_body or "",
                    upstream_status=exc.upstream_status,
                ),
                usage=_answer_usage(started, None),
            )
        except ValueError as exc:
            return ToolResult.failure(
                ToolError(code=ERROR_INVALID_INPUT, message=str(exc)),
                usage=_answer_usage(started, None),
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须把异常变成可展示原因
            # message 只留类型；原文放 detail，由调用方决定是否使用（审计不落 detail）。
            return ToolResult.failure(
                ToolError(
                    code=ERROR_INTERNAL,
                    message=f"回答工具异常（{type(exc).__name__}）",
                    detail=str(exc),
                ),
                usage=_answer_usage(started, None),
            )

        return ToolResult.success(
            payload,
            usage=_answer_usage(started, payload),
            evidence=evidence_from_query_response(payload),
        )

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        """显式探活（GET /health）；导入、注册、创建工具时都不会触发。"""
        started = time.perf_counter()
        try:
            probe = lightrag.probe_health()
        except Exception as exc:  # noqa: BLE001 - 探活失败也要返回结构化结果
            return Health(
                ok=False,
                detail=f"探活异常（{type(exc).__name__}）：{exc}",
                latency_ms=_elapsed_ms(started),
            )
        if not isinstance(probe, dict):
            return Health(ok=False, detail="探活返回了非对象结果", latency_ms=_elapsed_ms(started))
        status = probe.get("status_code")
        detail = str(probe.get("detail") or (f"HTTP {status}" if status else ""))
        return Health(
            ok=bool(probe.get("ok")),
            detail=detail,
            latency_ms=_elapsed_ms(started),
        )


def _answer_with_topic(
    query: str,
    topic_id: str,
    *,
    conversation_history: Any,
    user_prompt: Any,
    started: float,
) -> ToolResult:
    """P0-3B2b：先检索 → 只留本案文档 → bypass 生成，避免聊天跨案。"""
    from app.memory import runtime

    try:
        store = runtime.get_store()
    except Exception:  # noqa: BLE001
        store = None
    if store is None:
        return ToolResult.failure(
            ToolError(
                code=ERROR_TOPIC_UNAVAILABLE,
                message="资料集模型未启用，无法按资料集过滤知识库",
            ),
            usage=_answer_usage(started, None),
        )

    try:
        rows = store.documents_for_topic(topic_id)
    except Exception:  # noqa: BLE001
        rows = []
    allowed = {
        document_key(str(row.get("file_path") or ""))
        for row in rows
        if str(row.get("file_path") or "").strip()
    }
    if not allowed:
        return ToolResult.failure(
            ToolError(
                code=ERROR_TOPIC_NO_DOCUMENTS,
                message="本案尚未关联知识库文档；请先在文件页给文档关联资料集",
            ),
            usage=_answer_usage(started, None),
        )

    try:
        payload = lightrag.retrieve(query, mode="mix")
    except UpstreamError as exc:
        return ToolResult.failure(
            ToolError(
                code=ERROR_UPSTREAM,
                message=exc.message,
                retryable=_retryable(exc),
                detail=exc.upstream_body or "",
                upstream_status=exc.upstream_status,
            ),
            usage=_answer_usage(started, None),
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(
            ToolError(
                code=ERROR_INTERNAL,
                message=f"检索工具异常（{type(exc).__name__}）",
                detail=str(exc),
            ),
            usage=_answer_usage(started, None),
        )

    data = payload.get("data") if isinstance(payload, dict) else {}
    raw_chunks = data.get("chunks") if isinstance(data, dict) else []
    chunks: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in raw_chunks if isinstance(raw_chunks, list) else []:
        if not isinstance(chunk, dict):
            continue
        file_path = str(chunk.get("file_path") or "")
        content = str(chunk.get("content") or "").strip()
        if not content or document_key(file_path) not in allowed:
            continue
        key = file_path + "\n" + content
        if key in seen:
            continue
        seen.add(key)
        chunks.append(
            {
                "file_path": file_path,
                "content": content,
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "reference_id": str(chunk.get("reference_id") or ""),
            }
        )
    if not chunks:
        return ToolResult.failure(
            ToolError(
                code=ERROR_TOPIC_NO_EVIDENCE,
                message="本案已关联文档中没有检索到与问题相关的内容",
            ),
            usage=_answer_usage(started, None),
        )

    lines: List[str] = []
    references: List[Dict[str, Any]] = []
    evidence: List[Evidence] = []
    for index, chunk in enumerate(chunks, start=1):
        file_path = str(chunk["file_path"])
        content = str(chunk["content"])
        lines.append("[%d]（%s）%s" % (index, file_path, content))
        references.append(
            {
                "reference_id": str(chunk.get("reference_id") or index),
                "chunk_id": str(chunk.get("chunk_id") or ""),
                "file_path": file_path,
                "content": content[:2000],
            }
        )
        evidence.append(
            Evidence(
                evidence_id="reference:%d" % index,
                kind="reference",
                source_id=str(index),
                doc_name=file_path,
                text=content,
                metadata={
                    "reference_id": str(index),
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                },
            )
        )

    context = "\n\n".join(lines)
    if len(context) > MAX_TOPIC_CONTEXT_CHARS:
        context = context[:MAX_TOPIC_CONTEXT_CHARS].rstrip() + "\n…（资料已截断）"
    prompt = (
        "请只依据下面列出的资料回答问题。不得补充资料之外的事实、日期、金额、"
        "姓名、文号或法律结论；资料没有答案时，直接回答“资料中没有足够信息”。\n\n"
        "资料：\n" + context + "\n\n问题：" + query
    )

    kwargs: Dict[str, Any] = {}
    history = (
        conversation_history
        if isinstance(conversation_history, (list, tuple))
        else None
    )
    if history:
        kwargs["conversation_history"] = list(history)
    prompt_extra = user_prompt if isinstance(user_prompt, str) else ""
    if prompt_extra.strip():
        kwargs["user_prompt"] = prompt_extra

    try:
        reply = lightrag.query(
            prompt,
            mode="bypass",
            include_references=False,
            **kwargs,
        )
    except UpstreamError as exc:
        return ToolResult.failure(
            ToolError(
                code=ERROR_UPSTREAM,
                message=exc.message,
                retryable=_retryable(exc),
                detail=exc.upstream_body or "",
                upstream_status=exc.upstream_status,
            ),
            usage=_answer_usage(started, None),
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(
            ToolError(
                code=ERROR_INTERNAL,
                message=f"回答工具异常（{type(exc).__name__}）",
                detail=str(exc),
            ),
            usage=_answer_usage(started, None),
        )

    response = reply.get("response") if isinstance(reply, dict) else ""
    if not isinstance(response, str) or not response.strip():
        return ToolResult.failure(
            ToolError(code=ERROR_INTERNAL, message="知识库没有返回可用答案"),
            usage=_answer_usage(started, reply),
        )
    data_out = {"response": response, "references": references}
    return ToolResult.success(
        data_out,
        usage=_answer_usage(started, data_out),
        evidence=evidence,
    )


def _validate_answer_query(query: Any) -> Optional[ToolError]:
    if not isinstance(query, str) or len(query.strip()) < 3:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message="query 必须是至少 3 个字符的字符串",
        )
    return None


def _validate(
    query: Any,
    top_k: Optional[int],
    chunk_top_k: Optional[int],
    mode: Optional[str],
) -> Optional[ToolError]:
    if not isinstance(query, str) or len(query.strip()) < 3:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message="query 必须是至少 3 个字符的字符串",
        )
    for name, value in (("top_k", top_k), ("chunk_top_k", chunk_top_k)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return ToolError(
                code=ERROR_INVALID_INPUT,
                message=f"{name} 必须是正整数",
            )
    if mode is not None and mode not in lightrag.QUERY_MODES:
        return ToolError(
            code=ERROR_INVALID_INPUT,
            message=f"mode 必须是 {', '.join(lightrag.QUERY_MODES)} 之一",
        )
    return None


def _retryable(exc: UpstreamError) -> bool:
    return exc.kind == UPSTREAM_TIMEOUT or exc.upstream_status in RETRYABLE_UPSTREAM_STATUS


def _usage(
    started: float,
    mode: Optional[str],
    top_k: Optional[int],
    chunk_top_k: Optional[int],
) -> Dict[str, Any]:
    return {
        "elapsed_ms": _elapsed_ms(started),
        "mode": mode or lightrag.QUERY_MODE,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _score(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def evidence_from_query_data(payload: Any) -> List[Evidence]:
    """把 `/query/data` 的四类数据归一化成 Evidence。

    - 只读取，不修改 payload；
    - `evidence_id` 带类型前缀（entity:/relationship:/chunk:/reference:），保证跨类型唯一；
    - 空值、非字典项直接跳过，不抛异常（上游结构变化时仍能返回已有证据）。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []

    items: List[Evidence] = []
    seen: set[str] = set()

    def add(item: Evidence) -> None:
        if item.evidence_id in seen:
            return
        seen.add(item.evidence_id)
        items.append(item)

    for entity in data.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = _text(entity.get("entity_name")) or _text(entity.get("name"))
        if not name:
            continue
        metadata: Dict[str, Any] = {}
        entity_type = _text(entity.get("entity_type"))
        if entity_type:
            metadata["entity_type"] = entity_type
        add(
            Evidence(
                evidence_id=f"entity:{name}",
                kind="entity",
                source_id=name,
                doc_name=_text(entity.get("file_path")),
                text=_text(entity.get("description")),
                score=_score(entity.get("score")),
                metadata=metadata,
            )
        )

    for relationship in data.get("relationships") or []:
        if not isinstance(relationship, dict):
            continue
        src = _text(relationship.get("src_id")) or _text(relationship.get("source"))
        tgt = _text(relationship.get("tgt_id")) or _text(relationship.get("target"))
        if not src and not tgt:
            continue
        source_id = f"{src}->{tgt}"
        metadata = {}
        keywords = _text(relationship.get("keywords"))
        if keywords:
            metadata["keywords"] = keywords
        add(
            Evidence(
                evidence_id=f"relationship:{source_id}",
                kind="relationship",
                source_id=source_id,
                doc_name=_text(relationship.get("file_path")),
                text=_text(relationship.get("description")),
                score=_score(relationship.get("weight")),
                metadata=metadata,
            )
        )

    for index, chunk in enumerate(data.get("chunks") or []):
        if not isinstance(chunk, dict):
            continue
        chunk_id = _text(chunk.get("chunk_id"))
        reference_id = _text(chunk.get("reference_id"))
        metadata = {"reference_id": reference_id} if reference_id else {}
        add(
            Evidence(
                evidence_id=f"chunk:{chunk_id or reference_id or index}",
                kind="chunk",
                source_id=chunk_id,
                doc_name=_text(chunk.get("file_path")),
                text=_text(chunk.get("content")),
                score=_score(chunk.get("score")),
                metadata=metadata,
            )
        )

    for index, reference in enumerate(data.get("references") or []):
        if not isinstance(reference, dict):
            continue
        reference_id = _text(reference.get("reference_id"))
        add(
            Evidence(
                evidence_id=f"reference:{reference_id or index}",
                kind="reference",
                source_id=reference_id,
                doc_name=_text(reference.get("file_path")),
                text=_text(reference.get("content")),
                score=_score(reference.get("score")),
            )
        )

    return items


def _answer_usage(started: float, payload: Any) -> Dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    response = data.get("response")
    response_time = data.get("response_time")
    return {
        "elapsed_ms": _elapsed_ms(started),
        "answer_chars": len(response) if isinstance(response, str) else 0,
        "reference_count": len(_reference_items(payload)),
        "response_time_s": (
            float(response_time)
            if isinstance(response_time, (int, float)) and not isinstance(response_time, bool)
            else None
        ),
    }


def _reference_items(payload: Any) -> List[Dict[str, Any]]:
    """取 /query 的 references；兼容 `{references: [...]}` 与 `{data: {references: [...]}}`。"""
    if not isinstance(payload, dict):
        return []
    references = payload.get("references")
    if not isinstance(references, list):
        data = payload.get("data")
        references = data.get("references") if isinstance(data, dict) else []
    if not isinstance(references, list):
        return []
    return [item for item in references if isinstance(item, dict)]


def _reference_key(value: Any) -> str:
    """引用编号可能是字符串或数字；bool 不当数字用。"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def evidence_from_query_response(payload: Any) -> List[Evidence]:
    """把 `/query` 的 references 归一化成 Evidence。

    - `/query` 的 reference 只有 `reference_id` / `file_path` / `content`，
      `content` 可能为 null；为空时保留一条无正文的证据，不伪造片段；
    - `evidence_id` 统一为 `reference:<id>`，编号缺失时用序号；
    - 只读取，不修改 payload；非字典项跳过，不抛异常。
    """
    items: List[Evidence] = []
    seen: set[str] = set()

    for index, reference in enumerate(_reference_items(payload)):
        reference_id = _reference_key(reference.get("reference_id"))
        evidence_id = f"reference:{reference_id or index}"
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        items.append(
            Evidence(
                evidence_id=evidence_id,
                kind="reference",
                source_id=reference_id,
                doc_name=_text(reference.get("file_path")),
                text=_text(reference.get("content")),
                score=_score(reference.get("score")),
                metadata={"reference_id": reference_id} if reference_id else {},
            )
        )
    return items


TOOL = KnowledgeSearchTool()
ANSWER_TOOL = KnowledgeAnswerTool()
