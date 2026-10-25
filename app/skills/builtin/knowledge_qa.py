"""P2B：知识库问答技能（规则优先，单步）。

职责：
- 用规则判断“这个问题是否该走知识库问答”；
- 只调用一个已声明工具 `knowledge.answer`（不叠第二次检索）；
- 把 `/query` 的 references 映射成 `Citation`，供后续影子/验证使用。

刻意不做：
- 不接 `/chat`、不拼 ContextBuilder（P3 再接管生成）；
- 校验器由 Manager 在技能返回后运行，只记录、不阻断（P2D）；
- 不把问题、答案、证据正文写进 trace；trace 只留计数。

图片线索在 P2B 只给 0.45（低于路由阈值），由 fallback 继续走知识库问答；
P2C 的 `image_lookup` 会用更高分接管图片问题。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from app.plugins.contracts import Evidence, ToolContext
from app.services.context_builder import Citation
from app.skills.contracts import SkillError, SkillInput, SkillOutput, SkillSpec

SKILL_NAME = "knowledge_qa"
SKILL_VERSION = "0.1.0"

KNOWLEDGE_SCORE = 0.8
DEFAULT_SCORE = 0.6
IMAGE_ONLY_SCORE = 0.45

KNOWLEDGE_CUES = (
    "知识库",
    "资料",
    "文档",
    "项目",
    "总结",
    "引用",
    "来源",
    "原文",
)
IMAGE_CUES = (
    "图片",
    "照片",
    "截图",
    "图里",
    "这张图",
    "原图",
    "画面",
    "图中",
)

ERROR_EMPTY_ANSWER = "empty_answer"
ERROR_INVALID_RESPONSE = "invalid_response"

SKILL_SPEC = SkillSpec(
    name=SKILL_NAME,
    version=SKILL_VERSION,
    description="知识库问答：回答并返回引用编号",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
    output_schema={"type": "object"},
    required_tools=("knowledge.answer",),
    validators=("citation",),
    max_steps=1,
    timeout_s=300.0,
    enabled_by_default=True,
    keywords=KNOWLEDGE_CUES,
    priority=0,
)


class KnowledgeQaSkill:
    """规则 + 单次只读回答；无状态，可在多个 Manager 之间复用。"""

    spec = SKILL_SPEC

    def can_handle(self, request: SkillInput) -> float:
        question = getattr(request, "question", "")
        if not isinstance(question, str):
            return 0.0
        text = question.strip()
        if not text:
            return 0.0
        if any(cue in text for cue in KNOWLEDGE_CUES):
            return KNOWLEDGE_SCORE
        if any(cue in text for cue in IMAGE_CUES):
            return IMAGE_ONLY_SCORE
        return DEFAULT_SCORE

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        question = request.question.strip()
        emit = getattr(ctx, "emit", None)
        cancelled = getattr(ctx, "cancelled", None)
        tool_ctx = (
            ToolContext(emit=emit, cancelled=cancelled)
            if emit is not None or cancelled is not None
            else None
        )
        options = request.options if isinstance(request.options, dict) else {}
        args = {"query": question}
        history = options.get("conversation_history")
        if isinstance(history, (list, tuple)) and history:
            args["conversation_history"] = list(history)
        user_prompt = options.get("user_prompt")
        if isinstance(user_prompt, str) and user_prompt.strip():
            args["user_prompt"] = user_prompt
        topic_id = options.get("topic_id")
        if isinstance(topic_id, str) and topic_id.strip():
            args["topic_id"] = topic_id.strip()
        result = ctx.call_tool("knowledge.answer", args=args, tool_ctx=tool_ctx)
        if not result.ok:
            error = result.error
            # 只转交 code/message/retryable；ToolError.detail 可能带上游原文，留给调用方。
            return _error(
                error.code,
                error.message,
                retryable=bool(error.retryable),
            )

        data = result.data
        if not isinstance(data, dict):
            return _error(ERROR_INVALID_RESPONSE, "知识库返回了非对象结果")

        answer = data.get("response")
        if not isinstance(answer, str) or not answer.strip():
            return _error(ERROR_EMPTY_ANSWER, "知识库没有返回可用答案")

        evidence = list(result.evidence or [])
        citations = citations_from_evidence(evidence)
        response_time = _response_time(data.get("response_time"))

        usage: Dict[str, Any] = {
            "answer_chars": len(answer),
            "reference_count": len(evidence),
            "citation_count": len(citations),
            "response_time_s": response_time,
        }
        # P3C.2：本机工具会带 verified/规则码/用量；上游工具没有这些键，
        # 因此上游路径的 trace 与 usage 保持原样。
        tool_usage = result.usage if isinstance(result.usage, dict) else {}
        if "verified" in tool_usage:
            usage["verified"] = bool(tool_usage.get("verified"))
            codes = tool_usage.get("content_warning_codes") or []
            if not isinstance(codes, (list, tuple)):
                codes = []
            usage["content_warning_codes"] = [
                str(code) for code in codes if isinstance(code, str)
            ][:10]
            for key in (
                "done_reason",
                "prompt_eval_count",
                "eval_count",
                "total_duration_ms",
                "load_duration_ms",
                "prompt_eval_duration_ms",
                "eval_duration_ms",
            ):
                if key in tool_usage:
                    usage[key] = tool_usage[key]
        detail: Dict[str, Any] = {
            "answer_chars": usage["answer_chars"],
            "reference_count": usage["reference_count"],
            "citation_count": usage["citation_count"],
            "no_references": not evidence,
            "response_time_s": response_time,
        }
        if "verified" in tool_usage:
            detail["content_verified"] = usage["verified"]
            detail["content_warning_count"] = len(
                usage.get("content_warning_codes") or []
            )
        ctx.trace.detail.update(detail)

        return SkillOutput(
            status="ok" if evidence else "partial",
            answer=answer,
            evidence=evidence,
            citations=citations,
            usage=usage,
        )


def citations_from_evidence(evidence: Sequence[Evidence]) -> List[Citation]:
    """把证据映射成 `Citation`。

    `reference_id` 来自 Evidence.metadata（P2B 适配器）或 source_id（兼容旧形状）；
    缺失编号时使用稳定回退标签，不猜答案里没有的编号。

    P3B：本机生成的证据在 `metadata["citation_label"]` 里携带 ContextBuilder 的原始
    标签（例如 `[img-1]` / `[doc-1]`），这里原样透传；没有该字段时仍走原有规则，
    因此上游版行为不变。
    """
    citations: List[Citation] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, Evidence):
            continue
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)

        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        reference_id = _text(metadata.get("reference_id")) or _text(item.source_id)
        exact_label = _text(metadata.get("citation_label"))
        citations.append(
            Citation(
                citation_id=item.evidence_id,
                label=_normalize_label(exact_label) or _label(item, reference_id),
                kind=item.kind,
                reference_id=reference_id,
                doc_name=item.doc_name,
                file_id=_text(item.file_id) or _text(metadata.get("file_id")),
                image_id=_text(item.image_id) or _text(metadata.get("image_id")),
            )
        )
    return citations


def _normalize_label(value: str) -> str:
    text = _text(value)
    if not text:
        return ""
    return text if text.startswith("[") and text.endswith("]") else f"[{text}]"


def _label(item: Evidence, reference_id: str) -> str:
    if reference_id:
        return f"[{reference_id}]"
    fallback = _text(item.source_id) or item.evidence_id
    if fallback.startswith(f"{item.kind}:"):
        return f"[{fallback}]"
    return f"[{item.kind}:{fallback}]"


def _response_time(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _error(code: str, message: str, *, retryable: bool = False) -> SkillOutput:
    return SkillOutput(
        status="error",
        error=SkillError(code, message, retryable=retryable),
    )


SKILL = KnowledgeQaSkill()

__all__ = [
    "ERROR_EMPTY_ANSWER",
    "ERROR_INVALID_RESPONSE",
    "KNOWLEDGE_CUES",
    "IMAGE_CUES",
    "KnowledgeQaSkill",
    "SKILL",
    "SKILL_SPEC",
    "citations_from_evidence",
]
