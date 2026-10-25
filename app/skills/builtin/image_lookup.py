"""P2C：图片资料查找技能（规则优先，两通道）。

职责：
- 明确图片意图（图片/照片/截图/原图/画面/图里…）由本技能接管；
- 本地通道：`image.search` 查 gallery 的 caption / OCR 预览 / 关键词；
- 检索通道：`knowledge.search` 查 `/query/data`，再按 gallery 文档名分类出图片候选；
- 两通道按配额合并、按 image_id 去重，输出 `[img-n]` 引用与原图 URL。

P2E 路由收紧：
- 纯图片问题仍由本技能接管；
- 同时出现“文档/资料/说明/识别/OCR…”等内容线索时，本技能返回 0，
  让 `knowledge_qa` 回答（混合意图暂不返回图片清单，留给后续 mixed skill）。

刻意不做：
- 不接 `/chat`、不改 UI/设置页；
- 不调用 LLM 生成答案；answer 只是确定性的图片清单；
- 不把问题、答案、描述、预览、证据正文、文件路径写进 trace；
- 校验器由 Manager 在技能返回后运行，只记录、不阻断（P2D）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from app.plugins.contracts import Evidence
from app.services import image_evidence
from app.services.context_builder import Citation
from app.skills.contracts import SkillError, SkillInput, SkillOutput, SkillSpec

SKILL_NAME = "image_lookup"
SKILL_VERSION = "0.1.0"

IMAGE_SCORE = 0.9
IMAGE_CUES = (
    "图片",
    "照片",
    "截图",
    "原图",
    "画面",
    "图里",
    "图中",
    "图像",
    "这张图",
    "那张图",
    "找图",
    "搜图",
    "图片列表",
    "photo",
    "picture",
    "image",
    "screenshot",
)

#: P2E：图片问题里同时出现这些内容线索时，说明用户要的是“文档/资料/说明”，
#: 不是纯图片清单；返回 0 让 knowledge_qa 接管。刻意不包含“知识库”，
#: 因为“找一下知识库里的图片”仍然是图片请求。
KNOWLEDGE_CONTENT_CUES = (
    "文档",
    "资料",
    "说明",
    "介绍",
    "识别",
    "OCR",
    "原理",
    "总结",
    "引用",
    "来源",
    "原文",
)
MIXED_IMAGE_SCORE = 0.0

SKILL_SPEC = SkillSpec(
    name=SKILL_NAME,
    version=SKILL_VERSION,
    description="图片资料查找：本地索引 + 检索通道，返回图片证据与 [img-n] 引用",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
    output_schema={"type": "object"},
    required_tools=("image.search", "knowledge.search"),
    validators=("citation_labels",),
    max_steps=2,
    timeout_s=240.0,
    enabled_by_default=True,
    keywords=IMAGE_CUES,
    priority=10,
)


class ImageLookupSkill:
    """规则 + 两通道只读检索；无状态，可在多个 Manager 之间复用。"""

    spec = SKILL_SPEC

    def can_handle(self, request: SkillInput) -> float:
        question = getattr(request, "question", "")
        if not isinstance(question, str):
            return 0.0
        text = question.strip().casefold()
        if not text:
            return 0.0
        if not any(cue.casefold() in text for cue in IMAGE_CUES):
            return 0.0
        # P2E：图片 + 文档/资料/说明/识别等内容线索 = 混合意图，让 knowledge_qa 回答。
        if any(cue.casefold() in text for cue in KNOWLEDGE_CONTENT_CUES):
            return MIXED_IMAGE_SCORE
        return IMAGE_SCORE

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        question = request.question.strip()
        local = ctx.call_tool(
            "image.search",
            args={"query": question, "limit": image_evidence.DEFAULT_LIMIT},
        )
        retrieval = ctx.call_tool(
            "knowledge.search",
            args={"query": question, "chunk_top_k": image_evidence.DEFAULT_LIMIT},
        )

        local_ok = bool(local.ok)
        retrieval_ok = bool(retrieval.ok)
        local_error = "" if local_ok or local.error is None else local.error.code
        retrieval_error = (
            "" if retrieval_ok or retrieval.error is None else retrieval.error.code
        )

        local_images = (
            [
                item
                for item in (local.evidence or [])
                if isinstance(item, Evidence) and item.kind == image_evidence.IMAGE_KIND
            ]
            if local_ok
            else []
        )

        retrieval_images: List[Evidence] = []
        if retrieval_ok and retrieval.evidence:
            try:
                mapping = image_evidence.image_map()
                _, retrieval_images = image_evidence.classify_evidence(
                    retrieval.evidence, image_map=mapping
                )
            except Exception:                            # noqa: BLE001 - 分类失败不拖垮本地通道
                retrieval_images = []

        if not local_ok and not retrieval_ok:
            # 两路都不可用才算失败；只转交 code/message，不带 detail。
            error = local.error or retrieval.error
            _record_detail(
                ctx,
                question,
                local_ok,
                retrieval_ok,
                local_images,
                retrieval_images,
                [],
                local_error,
                retrieval_error,
            )
            return _error(
                error.code if error else "internal_error",
                error.message if error else "图片资料检索不可用",
                retryable=bool(error.retryable) if error else False,
            )

        merged = image_evidence.merge_images(
            local_images,
            retrieval_images,
            limit=image_evidence.DEFAULT_LIMIT,
        )
        citations = citations_from_images(merged)
        answer = _answer(merged, citations)

        if not merged:
            status = "partial"
        elif local_ok and retrieval_ok:
            status = "ok"
        else:
            status = "partial"

        usage: Dict[str, Any] = {
            "image_count": len(merged),
            "citation_count": len(citations),
            "local_count": len(local_images),
            "retrieval_count": len(retrieval_images),
            "local_ok": local_ok,
            "retrieval_ok": retrieval_ok,
            "answer_chars": len(answer),
        }
        _record_detail(
            ctx,
            question,
            local_ok,
            retrieval_ok,
            local_images,
            retrieval_images,
            merged,
            local_error,
            retrieval_error,
        )

        return SkillOutput(
            status=status,
            answer=answer,
            evidence=merged,
            citations=citations,
            usage=usage,
        )


def _record_detail(
    ctx: Any,
    question: str,
    local_ok: bool,
    retrieval_ok: bool,
    local_images: Sequence[Evidence],
    retrieval_images: Sequence[Evidence],
    merged: Sequence[Evidence],
    local_error: str,
    retrieval_error: str,
) -> None:
    """只写计数与错误码；不写问题、文档名、描述、预览或正文。"""
    detail = getattr(ctx, "trace", None)
    if detail is None or not hasattr(detail, "detail"):
        return
    detail.detail.update(
        {
            "query_chars": len(question),
            "local_ok": bool(local_ok),
            "retrieval_ok": bool(retrieval_ok),
            "local_count": len(local_images),
            "retrieval_count": len(retrieval_images),
            "image_count": len(merged),
            "citation_count": len(merged),
            "no_images": not merged,
            "local_error": local_error,
            "retrieval_error": retrieval_error,
        }
    )


def citations_from_images(evidence: Sequence[Evidence]) -> List[Citation]:
    """把图片证据映射成 `[img-n]` 引用；同一 image_id 只留一条。"""
    citations: List[Citation] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, Evidence) or item.kind != image_evidence.IMAGE_KIND:
            continue
        if item.evidence_id in seen:
            continue
        seen.add(item.evidence_id)

        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        gallery_meta = metadata.get("gallery")
        if not isinstance(gallery_meta, dict):
            gallery_meta = {}

        citations.append(
            Citation(
                citation_id=item.evidence_id,
                label=f"[img-{len(citations) + 1}]",
                kind=image_evidence.IMAGE_KIND,
                reference_id="",
                doc_name=item.doc_name,
                file_id=item.file_id,
                image_id=item.image_id,
                asset_paths=_strings(gallery_meta.get("asset_paths")),
                keywords=_strings(gallery_meta.get("keywords")),
                caption=str(gallery_meta.get("caption") or ""),
            )
        )
    return citations


def _answer(evidence: Sequence[Evidence], citations: Sequence[Citation]) -> str:
    if not evidence:
        return "没有找到匹配的图片资料。"
    lines = [f"找到 {len(citations)} 张相关图片："]
    for citation in citations:
        name = citation.doc_name or "（无文件名）"
        lines.append(f"{citation.label} {name}")
    return "\n".join(lines)


def _strings(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _error(code: str, message: str, *, retryable: bool = False) -> SkillOutput:
    return SkillOutput(
        status="error",
        error=SkillError(code, message, retryable=retryable),
    )


SKILL = ImageLookupSkill()

__all__ = [
    "IMAGE_CUES",
    "IMAGE_SCORE",
    "KNOWLEDGE_CONTENT_CUES",
    "MIXED_IMAGE_SCORE",
    "ImageLookupSkill",
    "SKILL",
    "SKILL_SPEC",
    "citations_from_images",
]
