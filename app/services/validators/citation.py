"""P1D：引用编号一致性校验（v1，纯函数、影子模式）。

只检查“答案里的编号是否来自本次上下文”，不做事实核查、不改答案。
v1 只解析数字编号：`[1]`、`[1,2]`、`[1-3]`、`【1】`；`[img-1]` 这类非数字标签
只统计、不解析，避免把普通方括号文字误判成引用。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Set, Tuple

from app.services.validators.base import Issue, ValidationReport

CITATION_VERSION = "1"
MAX_RANGE = 100

_MARKER_RE = re.compile(r"【([^】\n]{1,80})】|\[([^\]\n]{1,80})\]")
_TOKEN_RE = re.compile(r"^(?P<start>\d{1,3})(?:\s*[-–—]\s*(?P<end>\d{1,3}))?$")
_SPLIT_RE = re.compile(r"[,，、;；]+")
_NUMERIC_RE = re.compile(r"^\d{1,9}$")


class CitationValidator:
    """答案引用编号与 `citation_map` 的一致性校验。"""

    name = "citation"
    version = CITATION_VERSION

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
        allowed, total, non_numeric, valid = _normalize_citations(citations)
        if not valid:
            report.add(Issue("invalid_input", "citations 必须是映射或序列", "error"))
            return report

        cited_list, markers, formats = _parse_markers(answer, report)
        counts = Counter(cited_list)
        cited = set(counts)
        unknown = cited - allowed
        duplicates = {citation_id for citation_id, count in counts.items() if count > 1}

        report.stats.update(
            {
                "citation_count": total,
                "numeric_citation_count": len(allowed),
                "non_numeric_citation_count": non_numeric,
                "context_has_citations": total > 0,
                "cited_count": len(cited),
                "cited_ids": _sorted_ids(cited),
                "unknown_ids": _sorted_ids(unknown),
                "duplicate_ids": _sorted_ids(duplicates),
                "markers": markers,
                "formats": formats,
            }
        )

        if total == 0:
            if cited:
                report.add(
                    Issue(
                        "dangling_citation",
                        "上下文没有引用，但答案使用了引用标记",
                        "error",
                        ",".join(_sorted_ids(cited)),
                    )
                )
            return report

        for citation_id in _sorted_ids(unknown):
            report.add(
                Issue(
                    "unknown_citation",
                    f"答案引用了上下文中不存在的编号 [{citation_id}]",
                    "error",
                    citation_id,
                )
            )
        if not cited:
            report.add(
                Issue(
                    "missing_citation",
                    "上下文有引用，但答案没有使用任何引用标记",
                    "warning",
                )
            )
        for citation_id in _sorted_ids(duplicates):
            report.add(
                Issue(
                    "duplicate_citation",
                    f"引用编号 [{citation_id}] 在答案中重复出现",
                    "warning",
                    citation_id,
                )
            )
        return report


def _normalize_citations(
    citations: Any,
) -> Tuple[Set[str], int, int, bool]:
    """把 citation_map/序列收敛成数字编号集合。

    返回 `(允许编号, 条目总数, 非数字条目数, 输入是否合法)`。
    非数字标签（图片/文档）计入总数但不参与 v1 数字匹配。
    """
    if citations is None:
        return set(), 0, 0, True
    if hasattr(citations, "citation_map"):
        citations = citations.citation_map

    if isinstance(citations, Mapping):
        if any(key in citations for key in ("citation_id", "reference_id", "label")):
            entries: List[Any] = [citations]
        else:
            entries = list(citations.values())
    elif isinstance(citations, Sequence) and not isinstance(citations, (str, bytes)):
        entries = list(citations)
    else:
        return set(), 0, 0, False

    allowed: Set[str] = set()
    non_numeric = 0
    for entry in entries:
        if isinstance(entry, (str, int)) and not isinstance(entry, bool):
            numeric = _numeric_id(entry)
            if numeric:
                allowed.add(numeric)
            else:
                non_numeric += 1
            continue

        found = False
        for key in ("reference_id", "label", "citation_id"):
            if isinstance(entry, Mapping):
                value = entry.get(key)
            else:
                value = getattr(entry, key, None)
            numeric = _numeric_id(value)
            if numeric:
                allowed.add(numeric)
                found = True
        if not found:
            non_numeric += 1

    return allowed, len(entries), non_numeric, True


def _numeric_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if 0 <= value <= 999_999_999 else ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    elif text.startswith("【") and text.endswith("】"):
        text = text[1:-1].strip()
    if not _NUMERIC_RE.fullmatch(text):
        return ""
    return str(int(text))


def _parse_markers(
    answer: str, report: ValidationReport
) -> Tuple[List[str], int, Dict[str, int]]:
    """解析答案中的数字引用；畸形范围记 warning，普通方括号直接忽略。"""
    cited: List[str] = []
    markers = 0
    formats = {"bracket": 0, "corner": 0}

    for match in _MARKER_RE.finditer(answer):
        inner = (match.group(1) or match.group(2) or "").strip()
        if not inner:
            continue
        tokens = [token.strip() for token in _SPLIT_RE.split(inner) if token.strip()]
        if not tokens:
            continue

        parsed: List[str] = []
        usable = True
        for token in tokens:
            found = _TOKEN_RE.fullmatch(token)
            if found is None:
                # 不是纯数字/范围：当作普通方括号文字，整组忽略。
                usable = False
                break
            start = int(found.group("start"))
            end = int(found.group("end") or start)
            if end < start:
                report.add(
                    Issue(
                        "invalid_citation",
                        f"引用范围倒置：{token}",
                        "warning",
                        token,
                    )
                )
                usable = False
                break
            if end - start + 1 > MAX_RANGE:
                report.add(
                    Issue(
                        "invalid_citation",
                        f"引用范围过大：{token}",
                        "warning",
                        token,
                    )
                )
                usable = False
                break
            parsed.extend(str(value) for value in range(start, end + 1))

        if not usable:
            continue
        markers += 1
        formats["bracket" if match.group(2) is not None else "corner"] += 1
        cited.extend(parsed)

    return cited, markers, formats


def _sorted_ids(ids: Set[str]) -> List[str]:
    return sorted(ids, key=lambda value: (int(value), value))
