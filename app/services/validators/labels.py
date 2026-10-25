"""P2D：标签引用编号一致性校验（v1，纯函数、影子模式）。

`image_lookup` 使用 `[img-1]` 这类标签，而不是 P1D 的数字编号；本校验器只做
“答案里的标签是否来自本次上下文”的一致性检查，不做事实核查、不改答案。

边界与取舍：
- 只识别**声明过的标签前缀**（来自 `citations[].label`）以及默认前缀
  `img-` / `doc-`；普通方括号文字、Markdown 链接一律忽略；
- 支持 `[img-1]`、`[img-1,img-2]`、`【img-1】`；
- 编号统一去掉前导零（`img-01` → `img-1`），前缀统一小写；
- 报告只含编号、计数、issue code/severity/ref，不含答案或上下文正文。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Set, Tuple

from app.services.validators.base import Issue, ValidationReport

LABEL_VERSION = "1"

#: 没有上下文时也认得的标签前缀；用于区分“已知前缀的未知编号”（报错）
#: 与“普通方括号文字”（忽略）。声明过的标签前缀会自动加入这个集合。
DEFAULT_LABEL_PREFIXES: Tuple[str, ...] = ("img", "doc")

#: 标签编号上限（与数字校验器一致，避免异常长数字）。
MAX_LABEL_NUMBER = 999_999_999

_MARKER_RE = re.compile(r"【([^】\n]{1,80})】|\[([^\]\n]{1,80})\]")
_SPLIT_RE = re.compile(r"[,，、;；]+")
_LABEL_RE = re.compile(
    r"^(?P<prefix>[A-Za-z][A-Za-z0-9_]{0,31})-(?P<number>\d{1,9})$"
)


class LabelCitationValidator:
    """答案标签与 `citations[].label` 的一致性校验。"""

    name = "citation_labels"
    version = LABEL_VERSION

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

        report.stats.update(
            {"answer_chars": len(answer), "answer_nonempty": bool(answer.strip())}
        )
        if not answer.strip():
            report.add(Issue("empty_answer", "答案为空，无法校验引用", "error"))
            return report

        citations = payload.get("citation_map")
        if citations is None:
            citations = payload.get("citations")
        declared, total, non_label, valid = _normalize_citations(citations)
        if not valid:
            report.add(Issue("invalid_input", "citations 必须是映射或序列", "error"))
            return report

        known_prefixes = set(DEFAULT_LABEL_PREFIXES)
        known_prefixes.update(_prefix(label) for label in declared)
        cited_list, markers, formats = _parse_markers(answer, known_prefixes)
        counts = Counter(cited_list)
        cited = set(counts)
        unknown = cited - declared
        duplicates = {label for label, count in counts.items() if count > 1}

        report.stats.update(
            {
                "citation_count": total,
                "label_citation_count": len(declared),
                "non_label_citation_count": non_label,
                "context_has_citations": bool(declared),
                "cited_count": len(cited),
                "cited_ids": _sorted_ids(cited),
                "unknown_ids": _sorted_ids(unknown),
                "duplicate_ids": _sorted_ids(duplicates),
                "markers": markers,
                "formats": formats,
            }
        )

        if not declared:
            if cited:
                report.add(
                    Issue(
                        "dangling_citation",
                        "上下文没有标签引用，但答案使用了标签引用",
                        "error",
                        ",".join(_sorted_ids(cited)),
                    )
                )
            return report

        for label in _sorted_ids(unknown):
            report.add(
                Issue(
                    "unknown_citation",
                    f"答案引用了上下文中不存在的标签 [{label}]",
                    "error",
                    label,
                )
            )
        if not cited:
            report.add(
                Issue(
                    "missing_citation",
                    "上下文有标签引用，但答案没有使用任何标签引用",
                    "warning",
                )
            )
        for label in _sorted_ids(duplicates):
            report.add(
                Issue(
                    "duplicate_citation",
                    f"引用标签 [{label}] 在答案中重复出现",
                    "warning",
                    label,
                )
            )
        return report


def _normalize_citations(citations: Any) -> Tuple[Set[str], int, int, bool]:
    """把 citation_map/序列收敛成标签集合。

    返回 `(允许标签, 条目总数, 非标签条目数, 输入是否合法)`。
    """
    if citations is None:
        return set(), 0, 0, True
    if hasattr(citations, "citation_map"):
        citations = citations.citation_map

    if isinstance(citations, Mapping):
        if any(
            key in citations
            for key in ("citation_id", "reference_id", "label", "image_id")
        ):
            entries: List[Any] = [citations]
        else:
            entries = list(citations.values())
    elif isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        entries = list(citations)
    else:
        return set(), 0, 0, False

    declared: Set[str] = set()
    non_label = 0
    for entry in entries:
        label = _parse_label(_entry_label(entry))
        if label:
            declared.add(label)
        else:
            non_label += 1

    return declared, len(entries), non_label, True


def _entry_label(entry: Any) -> Any:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        return entry.get("label")
    return getattr(entry, "label", None)


def _parse_label(value: Any) -> str:
    """把 `[img-1]` / `【IMG-01】` / `img-1` 归一化成 `img-1`；不认识返回空串。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if (text.startswith("[") and text.endswith("]")) or (
        text.startswith("【") and text.endswith("】")
    ):
        text = text[1:-1].strip()
    found = _LABEL_RE.fullmatch(text)
    if found is None:
        return ""
    number = int(found.group("number"))
    if number > MAX_LABEL_NUMBER:
        return ""
    return f"{found.group('prefix').lower()}-{number}"


def _prefix(label: str) -> str:
    return label.rsplit("-", 1)[0]


def _parse_markers(
    answer: str, known_prefixes: Set[str]
) -> Tuple[List[str], int, Dict[str, int]]:
    """解析答案中的标签引用；未知前缀/普通方括号整组忽略。"""
    cited: List[str] = []
    markers = 0
    formats = {"bracket": 0, "corner": 0}

    for match in _MARKER_RE.finditer(answer):
        # Markdown 链接/图片：`[img-1](url)`、`![img-1](url)` 不当作正文引用。
        if _is_markdown_link(answer, match.end()):
            continue
        inner = (match.group(1) or match.group(2) or "").strip()
        if not inner:
            continue
        tokens = [token.strip() for token in _SPLIT_RE.split(inner) if token.strip()]
        if not tokens:
            continue

        parsed: List[str] = []
        usable = True
        for token in tokens:
            label = _parse_label(token)
            if not label or _prefix(label) not in known_prefixes:
                # 不是“已知前缀-数字”：当作普通方括号文字，整组忽略。
                usable = False
                break
            parsed.append(label)

        if not usable:
            continue
        markers += 1
        formats["bracket" if match.group(2) is not None else "corner"] += 1
        cited.extend(parsed)

    return cited, markers, formats


def _is_markdown_link(answer: str, end: int) -> bool:
    """判断方括号后紧跟的 `(...)` 是否像 Markdown 链接目标。

    只看目标是否以 URL/路径前缀开头，避免把 `[img-1]（说明）` 或
    `[img-1](见上文)` 这类正文误判成链接。
    """
    if answer[end : end + 1] != "(":
        return False
    close = answer.find(")", end + 1)
    if close < 0:
        return True
    target = answer[end + 1 : close].strip().lower()
    if not target:
        return False
    return target.startswith(
        ("http://", "https://", "/", "#", "./", "../", "www.", "mailto:")
    )


def _sorted_ids(labels: Set[str]) -> List[str]:
    return sorted(labels, key=lambda value: (_prefix(value), int(value.rsplit("-", 1)[1]), value))


__all__ = [
    "DEFAULT_LABEL_PREFIXES",
    "LABEL_VERSION",
    "LabelCitationValidator",
    "MAX_LABEL_NUMBER",
]
