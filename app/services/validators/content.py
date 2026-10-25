"""P3C.2：回答内容校验（确定性、只读、不联网）。

职责：
    · 把“答案是否可用”拆成可解释的规则码：空答案、未知编号、无引用、
      截断、重复、乱码、引用覆盖偏低、可疑数字、拒答等；
    · 只做**一致性检查**，不判断事实是否正确、不修改答案、不重新生成；
    · error = 硬失败（调用方回退上游或报错），warning = 保留答案并提示。

边界：
    · 输入只来自内存中的答案、`citation_map`、证据纯文本与 Ollama 用量；
    · 报告只含规则码、计数、编号/标签；**不复制答案正文或证据正文**；
    · 不写文件、不联网、不读配置；相同输入必须得到相同报告。

P3C.2 接入点：
    · `local_generation.answer()` / `iter_stream()` 在生成结束后调用；
    · 硬失败沿用 P3B/P3C 的回退策略：首 chunk 前回退上游，已输出后只报错；
    · warning 通过流式 `done` 事件的 `warnings` 字段暴露（只有规则码）。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Set, Tuple

from app.services.validators.base import Issue, ValidationReport
from app.services.validators.citation import CitationValidator

CONTENT_VERSION = "1"

#: 明确说明“资料不足”的回答不需要引用；与 P3A/P3B 的判定保持一致。
REFUSAL_MARKERS = (
    "没有足够信息",
    "资料中没有",
    "没有明确",
    "无法确定",
    "未提及",
    "没有具体提到",
    "无法回答",
    "没有相关信息",
)

#: 硬失败规则码；其余 issue 均为 warning。
HARD_CODES = frozenset(
    {
        "empty_answer",
        "invalid_input",
        "unknown_citation",
        "no_citation",
        "truncated",
        "repetition",
        "garbled",
    }
)

WARNING_CODES = frozenset(
    {
        "low_citation_coverage",
        "unsupported_number",
        "refusal_with_evidence",
        "too_short",
    }
)

#: 同一条规则最多报几条 issue，避免异常回答把报告撑大。
MAX_ISSUES_PER_RULE = 5

_MARKER_RE = re.compile(r"【([^】\n]{1,120})】|\[([^\]\n]{1,120})\]")
_SPLIT_RE = re.compile(r"[,，、;；]+")
_NUMBER_RANGE_RE = re.compile(r"^(?P<start>\d{1,3})(?:\s*[-–—]\s*(?P<end>\d{1,3}))?$")
_NAMED_LABEL_RE = re.compile(
    r"^(?:img-\d+|doc-\d+|"
    r"(?:chunk|entity|relationship|reference|document):[^\[\]\r\n]{1,120})$"
)
_SENTENCE_RE = re.compile(r"[。！？!?；;\n]+")
_NUMBER_RE = re.compile(r"(?<![\d.])(\d{2,6})(?![\d.])")


def is_refusal(text: str) -> bool:
    """回答是否明确说明“资料不足”；只做字符串包含判断。"""
    value = str(text or "")
    return any(marker in value for marker in REFUSAL_MARKERS)


def parse_citation_labels(text: str) -> List[str]:
    """按首次出现顺序返回去重后的引用标签（统一成方括号形式）。

    支持 `[1]`、`[1,2]`、`[1-3]`、`【1】`、`[img-1]`、`[doc-1]`、
    `[chunk:abc]`、`[entity:项目A]`；普通方括号文字（如 `[图片]`、
    Markdown 链接 `[链接](x)`）整组忽略。
    """
    labels: List[str] = []
    seen: Set[str] = set()
    for match in _MARKER_RE.finditer(str(text or "")):
        inner = (match.group(1) or match.group(2) or "").strip()
        if not inner:
            continue
        parsed = _parse_marker(inner)
        if parsed is None:
            continue
        for label in parsed:
            if label not in seen:
                seen.add(label)
                labels.append(label)
    return labels


def _parse_marker(inner: str) -> Optional[List[str]]:
    tokens = [token.strip() for token in _SPLIT_RE.split(inner) if token.strip()]
    if not tokens:
        return None

    parsed: List[str] = []
    for token in tokens:
        match = _NUMBER_RANGE_RE.fullmatch(token)
        if match is not None:
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            if end < start or end - start + 1 > 100:
                return None
            parsed.extend(f"[{value}]" for value in range(start, end + 1))
            continue
        if _NAMED_LABEL_RE.fullmatch(token):
            parsed.append(_normalize_label(token))
            continue
        # 任意一个 token 不像引用：整组当成普通方括号文字。
        return None
    return parsed or None


def _entry_label(entry: Any) -> str:
    if isinstance(entry, Mapping):
        for key in ("label", "reference_id"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    for key in ("label", "reference_id"):
        value = getattr(entry, key, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("【") and text.endswith("】"):
        text = text[1:-1].strip()
    elif text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not text:
        return ""
    if text.isdigit():
        text = str(int(text))
    else:
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)-0*(\d+)", text)
        if match is not None:
            text = f"{match.group(1)}-{int(match.group(2))}"
    return f"[{text}]"


def known_labels(citations: Any) -> Set[str]:
    """把 `citation_map` / 引用序列收敛成规范标签集合。"""
    labels: Set[str] = set()
    for entry in _citation_entries(citations):
        label = _normalize_label(_entry_label(entry))
        if label:
            labels.add(label)
    return labels


def _citation_entries(citations: Any) -> List[Any]:
    if citations is None:
        return []
    if hasattr(citations, "citation_map"):
        citations = citations.citation_map
    if isinstance(citations, Mapping):
        if any(
            key in citations
            for key in ("citation_id", "reference_id", "label", "image_id")
        ):
            return [citations]
        return list(citations.values())
    if isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        return list(citations)
    return []


def validate_content(
    *,
    answer: str,
    citations: Any = None,
    evidence_texts: Sequence[str] = (),
    done_reason: str = "",
    refusal: Optional[bool] = None,
    memory_context: bool = False,
) -> ValidationReport:
    """便捷函数：校验一次回答，返回 `ValidationReport`。

    `memory_context=True` 表示本轮注入了长期记忆。若没有任何知识库引用编号，
    “无引用”降级为 warning（答案可能完全来自用户记忆）；只要存在知识库引用，
    仍按原规则把无引用视为 error。
    """
    return ContentValidator().validate(
        {
            "answer": answer,
            "citation_map": citations,
            "evidence_texts": evidence_texts,
            "done_reason": done_reason,
            "refusal": refusal,
            "memory_context": bool(memory_context),
        }
    )


class ContentValidator:
    """答案内容校验器（v1）；`ok = 没有 error`。"""

    name = "content"
    version = CONTENT_VERSION

    def validate(self, payload: Mapping[str, Any]) -> ValidationReport:
        report = ValidationReport(validator=self.name, version=self.version)
        if not isinstance(payload, Mapping):
            report.add(Issue("invalid_input", "payload 必须是映射", "error"))
            return report

        answer = payload.get("answer")
        if not isinstance(answer, str):
            report.stats.update({"answer_chars": 0, "answer_nonempty": False})
            report.add(Issue("invalid_input", "answer 必须是字符串", "error"))
            return report

        text = answer.strip()
        report.stats.update(
            {"answer_chars": len(answer), "answer_nonempty": bool(text)}
        )
        if not text:
            report.add(Issue("empty_answer", "答案为空", "error"))
            return report

        citations = payload.get("citation_map")
        if citations is None:
            citations = payload.get("citations")
        known = known_labels(citations)
        cited = parse_citation_labels(text)
        unknown = [label for label in cited if label not in known]
        unknown = _merge_numeric_unknown(
            unknown, answer, citations
        )
        counts = Counter(cited)
        duplicates = sorted(
            (label for label, count in counts.items() if count > 1),
            key=_label_sort_key,
        )

        refusal_flag = payload.get("refusal")
        if not isinstance(refusal_flag, bool):
            refusal_flag = is_refusal(text)
        memory_context = bool(payload.get("memory_context"))

        evidence_texts = _clean_texts(payload.get("evidence_texts"))
        evidence_blob = _normalize_numbers("\n".join(evidence_texts))
        evidence_numbers = set(_NUMBER_RE.findall(evidence_blob))
        number_candidates = _number_candidates(text)
        unsupported = [
            number for number in number_candidates if number not in evidence_numbers
        ]

        sentence_stats = _sentence_stats(text)
        repetition_stats = _repetition_stats(text)
        garbled_chars = _garbled_chars(text)
        done = str(payload.get("done_reason") or "").strip().lower()

        report.stats.update(
            {
                "citation_count": len(_citation_entries(citations)),
                "known_label_count": len(known),
                "cited_count": len(cited),
                "cited_labels": cited[:20],
                "unknown_labels": unknown[:MAX_ISSUES_PER_RULE],
                "duplicate_labels": duplicates[:MAX_ISSUES_PER_RULE],
                "context_has_citations": bool(known),
                "refusal": refusal_flag,
                "done_reason": done,
                "sentence_count": sentence_stats["sentence_count"],
                "cited_sentence_count": sentence_stats["cited_sentence_count"],
                "citation_coverage": sentence_stats["citation_coverage"],
                "number_count": len(number_candidates),
                "unsupported_numbers": unsupported[:MAX_ISSUES_PER_RULE],
                "ngram_total": repetition_stats["ngram_total"],
                "ngram_repeat_ratio": repetition_stats["ngram_repeat_ratio"],
                "max_line_repeat": repetition_stats["max_line_repeat"],
                "garbled_chars": garbled_chars,
            }
        )

        if unknown:
            for label in unknown[:MAX_ISSUES_PER_RULE]:
                report.add(
                    Issue(
                        "unknown_citation",
                        "答案引用了上下文中不存在的资料编号",
                        "error",
                        label,
                    )
                )
        if not cited and not refusal_flag:
            # 只有“本轮没有知识库引用”且注入了长期记忆时，才允许答案来自记忆；
            # 有知识库证据却完全不引用，仍视为 error，避免记忆削弱 KB 引用约束。
            severity = "warning" if (memory_context and not known) else "error"
            report.add(
                Issue(
                    "no_citation",
                    "答案没有引用资料，也没有明确说明资料不足",
                    severity,
                )
            )
        if done == "length":
            report.add(
                Issue(
                    "truncated",
                    "答案达到生成长度上限（done_reason=length）",
                    "error",
                )
            )
        if repetition_stats["repetitive"]:
            report.add(
                Issue(
                    "repetition",
                    "答案出现重复循环",
                    "error",
                )
            )
        if garbled_chars > 2 or (
            len(text) >= 20 and garbled_chars / max(1, len(text)) > 0.02
        ):
            report.add(
                Issue(
                    "garbled",
                    "答案包含乱码或不可见控制字符",
                    "error",
                )
            )

        if refusal_flag and known:
            report.add(
                Issue(
                    "refusal_with_evidence",
                    "资料可用但答案选择拒答",
                    "warning",
                )
            )
        if (
            not refusal_flag
            and cited
            and sentence_stats["sentence_count"] >= 3
            and sentence_stats["citation_coverage"] < 0.5
        ):
            report.add(
                Issue(
                    "low_citation_coverage",
                    "带引用的句子占比偏低",
                    "warning",
                )
            )
        if evidence_texts:
            for number in unsupported[:MAX_ISSUES_PER_RULE]:
                report.add(
                    Issue(
                        "unsupported_number",
                        "答案中的数字未在资料中出现",
                        "warning",
                        number,
                    )
                )
        if not refusal_flag and len(text) < 20:
            report.add(Issue("too_short", "答案过短", "warning"))
        return report


def error_codes(report: ValidationReport) -> List[str]:
    """按出现顺序返回去重后的 error 规则码。"""
    return _codes(report, "error")


def warning_codes(report: ValidationReport) -> List[str]:
    """按出现顺序返回去重后的 warning 规则码。"""
    return _codes(report, "warning")


def failure_kind(report: ValidationReport) -> str:
    """把首个硬失败规则码映射成调用方的错误类别。"""
    codes = error_codes(report)
    if not codes:
        return "local_content_invalid"
    first = codes[0]
    if first in ("unknown_citation", "no_citation"):
        return "local_unverified"
    if first == "empty_answer":
        return "local_empty"
    if first == "truncated":
        return "local_truncated"
    if first == "repetition":
        return "local_repetitive"
    if first == "garbled":
        return "local_garbled"
    return "local_content_invalid"


def failure_message(report: ValidationReport) -> str:
    """面向用户的失败文案；不含答案、编号或证据正文。"""
    codes = error_codes(report)
    first = codes[0] if codes else ""
    if first in ("unknown_citation", "no_citation"):
        return "本机回答没有通过引用校验（未知编号或无依据）"
    if first == "empty_answer":
        return "本地模型返回空回答"
    if first == "truncated":
        return "本机回答达到长度上限，内容不完整"
    if first == "repetition":
        return "本机回答出现重复循环，已停止展示"
    if first == "garbled":
        return "本机回答包含乱码，未通过内容校验"
    return "本机回答没有通过内容校验"


def _codes(report: ValidationReport, severity: str) -> List[str]:
    result: List[str] = []
    for issue in report.issues:
        if issue.severity == severity and issue.code not in result:
            result.append(issue.code)
    return result


def _merge_numeric_unknown(
    unknown: List[str], answer: str, citations: Any
) -> List[str]:
    """复用 P1D 的数字解析，补上自定义解析可能漏掉的数字编号。"""
    try:
        report = CitationValidator().validate(
            {"answer": answer, "citation_map": citations}
        )
    except Exception:  # noqa: BLE001 - 校验器故障不能拖垮回答
        return unknown
    values = list(unknown)
    for citation_id in report.stats.get("unknown_ids", []) or []:
        label = f"[{citation_id}]"
        if label not in values:
            values.append(label)
    return values


def _clean_texts(value: Any) -> List[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _normalize_numbers(text: str) -> str:
    """去掉数字中的千分位逗号，便于和答案里的写法对齐。"""
    return re.sub(r"(?<=\d)[,，](?=\d)", "", str(text or ""))


def _number_candidates(text: str) -> List[str]:
    """提取答案中的 2–6 位数字；引用标记先整体去掉。"""
    stripped = _normalize_numbers(_MARKER_RE.sub(" ", text))
    seen: Set[str] = set()
    result: List[str] = []
    for match in _NUMBER_RE.finditer(stripped):
        number = match.group(1)
        if number not in seen:
            seen.add(number)
            result.append(number)
    return result


def _sentence_stats(text: str) -> Dict[str, Any]:
    sentences = [
        item.strip()
        for item in _SENTENCE_RE.split(text)
        if len(item.strip()) >= 8
    ]
    if not sentences:
        return {
            "sentence_count": 0,
            "cited_sentence_count": 0,
            "citation_coverage": 0.0,
        }
    cited = sum(1 for item in sentences if parse_citation_labels(item))
    return {
        "sentence_count": len(sentences),
        "cited_sentence_count": cited,
        "citation_coverage": round(cited / len(sentences), 3),
    }


def _repetition_stats(text: str) -> Dict[str, Any]:
    normalized = re.sub(r"\s+", "", text)
    grams = [
        normalized[index : index + 8]
        for index in range(max(0, len(normalized) - 7))
    ]
    ratio = 0.0
    if len(grams) >= 40:
        ratio = 1.0 - len(set(grams)) / len(grams)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    max_line_repeat = max(Counter(lines).values()) if lines else 0
    return {
        "ngram_total": len(grams),
        "ngram_repeat_ratio": round(ratio, 3),
        "max_line_repeat": max_line_repeat,
        "repetitive": ratio > 0.35
        or (max_line_repeat >= 3 and len(text) >= 100),
    }


def _garbled_chars(text: str) -> int:
    count = 0
    for char in text:
        if char == "�":
            count += 1
            continue
        if ord(char) < 32 and char not in "\n\r\t":
            count += 1
    return count


def _label_sort_key(label: str) -> Tuple[int, str]:
    inner = label[1:-1] if label.startswith("[") and label.endswith("]") else label
    try:
        return (int(inner), label)
    except ValueError:
        return (999_999_999, label)


__all__ = [
    "CONTENT_VERSION",
    "ContentValidator",
    "HARD_CODES",
    "REFUSAL_MARKERS",
    "WARNING_CODES",
    "error_codes",
    "failure_kind",
    "failure_message",
    "is_refusal",
    "known_labels",
    "parse_citation_labels",
    "validate_content",
    "warning_codes",
]
