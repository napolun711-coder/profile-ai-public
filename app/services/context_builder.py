"""P1C：Context Builder v1 —— 把证据组装成可追溯的上下文（纯函数、影子模式）。

边界（故意写死）：
    · **只组装**：不路由、不抓取、不调模型、不写文件；
    · 输入是已经拿到的 `Evidence`（P0 的检索结果、P1B 的 gallery 证据），
      不在这里发起检索，也不凭空新增没有召回的资料；
    · 相同输入必须得到完全相同的输出（不按分数重排、不做随机抽样）。

引用编号：
    · 带 `reference_id` 的证据（片段/引用）**原样继承**上游编号，例如 `[3]`；
      同一文档的多个 chunk 会共享同一个 `[3]`，这正是 LightRAG 的语义，
      因此 `citation_map` 的键是 chunk_id 而不是 reference_id —— 按 reference_id
      去重会把同一文档的正文片段吞掉；
    · reference 的编号在真实适配器里放在 `source_id`（chunk 才放在 `metadata`），
      两种形状都兼容；
    · 没有 `reference_id` 时回退成 `[<kind>:<source_id>]`；
    · 独立图片 `[img-1]`、独立文档 `[doc-1]`。

预算：
    · 按固定分组顺序贪心装入，单条正文截断到 `per_item_chars`；
    · 不超过 `max_items`，最终 `context_text`（含组标题）的估算 token 不超过 `total`；
    · 没装入的只记录 `evidence_id` 与原因，**不记录正文**。

本模块不接任何生产链路：P1C 只被影子脚本与测试导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.plugins.contracts import Evidence
from app.services import evidence as evidence_service

CONTEXT_VERSION = 1

DEFAULT_SYSTEM_PROMPT = (
    "你是本地知识库助手。只依据提供的资料回答；"
    "引用资料时使用给定的编号；资料不足时明确说明。"
)

#: 固定分组顺序（未知类型排在最后，按名称排序，保证确定性）
GROUP_ORDER = ("chunk", "reference", "image", "entity", "relationship")
GROUP_TITLES = {
    "chunk": "资料片段",
    "reference": "参考资料",
    "image": "图片资料",
    "entity": "实体",
    "relationship": "关系",
    "document": "文档资料",
}

#: 取完整纯文本时用的“无上限”（复用 P1B 的净化规则，不在这里再写一份）
_NO_LIMIT = 2**31 - 1


@dataclass(frozen=True)
class RuleSet:
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    require_citations: bool = True
    image_label_prefix: str = "img-"
    doc_label_prefix: str = "doc-"


@dataclass(frozen=True)
class TokenBudget:
    total: int = 4000
    per_item_chars: int = 800
    max_items: int = 40

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("TokenBudget.total 不能为负数")
        if self.per_item_chars < 1:
            raise ValueError("TokenBudget.per_item_chars 必须大于 0")
        if self.max_items < 0:
            raise ValueError("TokenBudget.max_items 不能为负数")


@dataclass(frozen=True)
class Citation:
    citation_id: str
    label: str
    kind: str
    reference_id: str = ""
    doc_name: str = ""
    file_id: str = ""
    image_id: str = ""
    origin_url: str = ""
    asset_paths: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    caption: str = ""


@dataclass(frozen=True)
class BuiltContext:
    version: int
    system_prompt: str
    context_text: str
    citation_map: Dict[str, Citation]
    groups: Dict[str, List[Evidence]]
    token_estimate: int
    dropped: List[Dict[str, str]]
    trace: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Candidate:
    group: str
    item: Evidence
    citation_id: str


# ============================================================
# 基础工具
# ============================================================

def estimate_tokens(text: str) -> int:
    """保守估算：CJK 字符按 1 token，其他字符按 4 字符 ≈ 1 token，向上取整。"""
    value = str(text or "")
    cjk = 0
    other = 0
    for char in value:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
    )


def _str(value: Any) -> str:
    """把可能为 None 的字符串字段收敛成可安全处理的字符串。"""
    return str(value or "").strip()


def _reference_id(item: Evidence) -> str:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    value = metadata.get("reference_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if item.kind == "reference":
        # P0 适配器把 reference 的编号放在 source_id（chunk 才放在 metadata），
        # 这里兼容两种形状，否则真实检索数据里的 reference 无法并进同编号的 chunk。
        return _str(item.source_id)
    return ""


def _gallery_meta(item: Evidence) -> Dict[str, Any]:
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    gallery = metadata.get("gallery")
    return gallery if isinstance(gallery, dict) else {}


def _strings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def _citation_id(item: Evidence) -> str:
    """citation_map 的键：chunk 用 chunk_id（同一文档的多个片段不能互相覆盖）。"""
    if item.kind == "chunk":
        return _str(item.source_id) or item.evidence_id
    reference_id = _reference_id(item)
    if reference_id:
        return reference_id
    if item.kind == "image" and _str(item.image_id):
        return _str(item.image_id)
    return _str(item.source_id) or item.evidence_id


def _label(item: Evidence, rules: RuleSet, counters: Dict[str, int]) -> str:
    reference_id = _reference_id(item)
    if reference_id:
        return f"[{reference_id}]"
    if item.kind == "image":
        counters["image"] = counters.get("image", 0) + 1
        return f"[{rules.image_label_prefix}{counters['image']}]"
    if item.kind == "document":
        counters["document"] = counters.get("document", 0) + 1
        return f"[{rules.doc_label_prefix}{counters['document']}]"
    fallback = _str(item.source_id) or _str(item.evidence_id)
    if fallback.startswith(f"{item.kind}:"):
        return f"[{fallback}]"
    return f"[{item.kind}:{fallback}]"


def _citation(item: Evidence, citation_id: str, label: str) -> Citation:
    gallery = _gallery_meta(item)
    return Citation(
        citation_id=citation_id,
        label=label,
        kind=item.kind,
        reference_id=_reference_id(item),
        doc_name=_str(item.doc_name),
        file_id=_str(item.file_id) or str(gallery.get("file_id") or ""),
        image_id=_str(item.image_id) or str(gallery.get("image_id") or ""),
        asset_paths=_strings(gallery.get("asset_paths")),
        keywords=_strings(gallery.get("keywords")),
        caption=str(gallery.get("caption") or ""),
    )


def _merge_citation(old: Citation, item: Evidence) -> Citation:
    """把后到的证据（通常是文件级 reference）合并进已有引用，不重复渲染正文。"""
    gallery = _gallery_meta(item)
    return replace(
        old,
        reference_id=old.reference_id or _reference_id(item),
        doc_name=old.doc_name or _str(item.doc_name),
        file_id=old.file_id or _str(item.file_id) or str(gallery.get("file_id") or ""),
        image_id=old.image_id or _str(item.image_id) or str(gallery.get("image_id") or ""),
        asset_paths=old.asset_paths or _strings(gallery.get("asset_paths")),
        keywords=old.keywords or _strings(gallery.get("keywords")),
        caption=old.caption or str(gallery.get("caption") or ""),
    )


# ============================================================
# Gallery 关联（I/O 由调用方注入）
# ============================================================

def enrich_with_gallery(
    evidence: Sequence[Evidence],
    lookup: Optional[Callable[[str], Optional[Dict[str, Any]]]],
) -> List[Evidence]:
    """用 gallery 索引补充已召回证据的 `file_id` / `image_id` / 描述 / 原图路径。

    · 只补充、**不新增**：没有召回的文档不会因为索引里有就进入上下文；
    · 不修改输入对象；`lookup` 抛异常按未命中处理；
    · 同一个文档名一次构建只查一次（chunks 往往来自同一份文档）。
    """
    items = list(evidence or [])
    if not callable(lookup):
        return items

    cache: Dict[str, Optional[Dict[str, Any]]] = {}
    enriched: List[Evidence] = []
    for item in items:
        if not isinstance(item, Evidence):
            enriched.append(item)
            continue
        name = _str(item.doc_name)
        if not name:
            enriched.append(item)
            continue
        key = name.lower()
        if key not in cache:
            try:
                hit = lookup(name)
            except Exception:                            # noqa: BLE001 - 影子关联不许阻断
                hit = None
            cache[key] = hit if isinstance(hit, dict) else None
        entry = cache[key]
        gallery = (entry or {}).get("evidence")
        if not isinstance(gallery, dict) or not gallery:
            enriched.append(item)
            continue

        metadata = dict(item.metadata) if isinstance(item.metadata, dict) else {}
        metadata["gallery"] = {
            "kind": str(gallery.get("kind") or ""),
            "caption": str(gallery.get("caption") or ""),
            "ocr_preview": str(gallery.get("ocr_preview") or ""),
            "asset_paths": list(_strings(gallery.get("asset_paths"))),
            "keywords": list(_strings(gallery.get("keywords"))),
            "result_pointer": str(gallery.get("result_pointer") or ""),
            "content_pointer": str(gallery.get("content_pointer") or ""),
        }
        enriched.append(
            replace(
                item,
                file_id=_str(item.file_id) or str((entry or {}).get("file_id") or ""),
                image_id=_str(item.image_id) or str(gallery.get("image_id") or ""),
                metadata=metadata,
            )
        )
    return enriched


# ============================================================
# 组装
# ============================================================

def build_context(
    *,
    question: str,
    evidence: Sequence[Evidence],
    rules: Optional[RuleSet] = None,
    output_format: str = "",
    budget: Optional[TokenBudget] = None,
) -> BuiltContext:
    """把证据组装成 `BuiltContext`；任何坏输入都降级，不抛异常。"""
    rules = rules or RuleSet()
    budget = budget or TokenBudget()
    raw = list(evidence or [])

    dropped: List[Dict[str, str]] = []
    items: List[Evidence] = []
    for obj in raw:
        if isinstance(obj, Evidence):
            items.append(obj)
        else:
            dropped.append(
                {
                    "evidence_id": str(getattr(obj, "evidence_id", "") or type(obj).__name__),
                    "reason": "invalid",
                }
            )

    by_kind: Dict[str, List[Evidence]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    order = [kind for kind in GROUP_ORDER if kind in by_kind]
    order.extend(sorted(kind for kind in by_kind if kind not in GROUP_ORDER))

    counters: Dict[str, int] = {}
    citations: Dict[str, Citation] = {}
    candidates: List[_Candidate] = []
    ref_to_cids: Dict[str, List[str]] = {}
    seen_evidence: set[str] = set()
    merged = 0

    for kind in order:
        for item in by_kind[kind]:
            if item.evidence_id in seen_evidence:
                dropped.append({"evidence_id": item.evidence_id, "reason": "duplicate"})
                continue
            seen_evidence.add(item.evidence_id)

            reference_id = _reference_id(item)
            if kind == "reference" and reference_id:
                # 同一文档的 chunk 已经渲染过：只把文件级信息（描述/原图）并进去，
                # 不重复渲染一条只有文件名的引用。
                targets = ref_to_cids.get(reference_id) or []
                if targets:
                    for cid in targets:
                        citations[cid] = _merge_citation(citations[cid], item)
                    merged += 1
                    continue

            cid = _citation_id(item)
            if cid in citations:
                citations[cid] = _merge_citation(citations[cid], item)
                merged += 1
                continue

            label = _label(item, rules, counters)
            citations[cid] = _citation(item, cid, label)
            candidates.append(_Candidate(group=kind, item=item, citation_id=cid))
            if reference_id and kind == "chunk":
                ref_to_cids.setdefault(reference_id, []).append(cid)

    _keep_document_info_once(candidates, citations)
    included, fragments, budget_dropped, truncated = _fit_budget(
        candidates, citations, budget, show_labels=rules.require_citations
    )
    dropped.extend(budget_dropped)

    context_text = _render(included, fragments)
    groups: Dict[str, List[Evidence]] = {}
    used_citations: Dict[str, Citation] = {}
    for candidate in included:
        groups.setdefault(candidate.group, []).append(candidate.item)
        used_citations[candidate.citation_id] = citations[candidate.citation_id]

    system_prompt = _system_prompt(rules, output_format)
    trace: Dict[str, Any] = {
        "version": CONTEXT_VERSION,
        "input_count": len(raw),
        "valid_count": len(items),
        "invalid_count": len(raw) - len(items),
        "input_by_kind": {kind: len(by_kind.get(kind, [])) for kind in order},
        "included_count": len(included),
        "included_by_kind": {kind: len(groups.get(kind, [])) for kind in order},
        "citations": len(used_citations),
        "merged": merged,
        "truncated": truncated,
        "gallery_matches": sum(
            1
            for citation in used_citations.values()
            if citation.image_id or citation.asset_paths
        ),
        "question_chars": len(str(question or "").strip()),
        "output_format": bool(str(output_format or "").strip()),
        "budget_tokens": budget.total,
        "per_item_chars": budget.per_item_chars,
        "max_items": budget.max_items,
        "system_tokens": estimate_tokens(system_prompt),
    }
    return BuiltContext(
        version=CONTEXT_VERSION,
        system_prompt=system_prompt,
        context_text=context_text,
        citation_map=used_citations,
        groups=groups,
        token_estimate=estimate_tokens(context_text),
        dropped=dropped,
        trace=trace,
    )


def _keep_document_info_once(
    candidates: List[_Candidate], citations: Dict[str, Citation]
) -> None:
    """同一文档的多个片段只保留一份描述/原图信息，避免每个 chunk 重复一遍。

    引用编号仍按上游 `reference_id` 共享；被清掉的只是 Citation 里的文件级附加信息。
    """
    seen: set[str] = set()
    for candidate in candidates:
        citation = citations[candidate.citation_id]
        reference_id = citation.reference_id
        if not reference_id:
            continue
        if reference_id in seen:
            citations[candidate.citation_id] = replace(
                citation,
                image_id="",
                asset_paths=(),
                keywords=(),
                caption="",
            )
        else:
            seen.add(reference_id)


def _fit_budget(
    candidates: List[_Candidate],
    citations: Dict[str, Citation],
    budget: TokenBudget,
    *,
    show_labels: bool,
) -> Tuple[List[_Candidate], List[str], List[Dict[str, str]], int]:
    """按顺序贪心装入；预算按最终渲染文本（含组标题）计算，被挡下的只记 id 与原因。"""
    included: List[_Candidate] = []
    fragments: List[str] = []
    dropped: List[Dict[str, str]] = []
    truncated = 0

    for position, candidate in enumerate(candidates):
        if len(included) >= budget.max_items:
            dropped.append({"evidence_id": candidate.item.evidence_id, "reason": "max_items"})
            continue
        fragment, was_truncated = _fragment(
            candidate,
            citations[candidate.citation_id],
            budget.per_item_chars,
            show_labels=show_labels,
        )
        # 按最终渲染文本试算：组标题和分隔符也要计入，否则边界会越过 total。
        trial = _render(included + [candidate], fragments + [fragment])
        if estimate_tokens(trial) > budget.total:
            # 严格按优先级：第一条装不下就停，后面的低优先级证据不再插队。
            dropped.extend(
                {"evidence_id": rest.item.evidence_id, "reason": "budget"}
                for rest in candidates[position:]
            )
            break
        included.append(candidate)
        fragments.append(fragment)
        truncated += 1 if was_truncated else 0

    return included, fragments, dropped, truncated


def _join(fragments: List[str]) -> str:
    return "\n\n".join(fragment for fragment in fragments if fragment)


def _fragment(
    candidate: _Candidate,
    citation: Citation,
    per_item_chars: int,
    *,
    show_labels: bool,
) -> Tuple[str, bool]:
    item = candidate.item
    display = _str(item.doc_name) or _str(item.source_id) or citation.citation_id
    header = f"{citation.label} {display}" if show_labels else display
    lines = [header.rstrip()]

    plain = evidence_service.preview(item.text, limit=_NO_LIMIT)
    was_truncated = len(plain) > per_item_chars
    if plain:
        lines.append(plain[:per_item_chars])

    if citation.image_id or citation.asset_paths:
        if citation.caption:
            caption = evidence_service.preview(citation.caption, limit=_NO_LIMIT)
            lines.append(f"描述：{caption[:per_item_chars]}")
        if citation.asset_paths:
            lines.append("原图：" + "、".join(citation.asset_paths))
        elif citation.image_id:
            lines.append(f"图片：{citation.image_id}")

    return "\n".join(lines), was_truncated


def _render(included: List[_Candidate], fragments: List[str]) -> str:
    if not included:
        return ""
    parts: List[str] = []
    current = ""
    for candidate, fragment in zip(included, fragments):
        if candidate.group != current:
            current = candidate.group
            title = GROUP_TITLES.get(current, current)
            parts.append(f"## {title}")
        parts.append(fragment)
    return _join(parts)


def _system_prompt(rules: RuleSet, output_format: str) -> str:
    prompt = str(rules.system_prompt or "").strip() or DEFAULT_SYSTEM_PROMPT
    if rules.require_citations and "引用" not in prompt:
        prompt += "\n引用资料时使用上下文中的方括号编号。"
    extra = str(output_format or "").strip()
    if extra:
        prompt += f"\n\n输出格式要求：\n{extra}"
    return prompt
