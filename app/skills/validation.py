"""P2D：Skill Validator 注册表与运行器（只读、脱敏、非阻断）。

职责：
- 把 `SkillSpec.validators` 声明的校验器跑一遍，返回**只含计数/编号/错误码**的摘要；
- 校验器异常、未知校验器、非法报告都在这里收敛成结构化结果，不向 Manager 抛；
- 不修改答案、不改变命令状态、不触发 fallback。

刻意不做：
- 不记录 `Issue.message`、答案、证据正文或上下文；摘要只留 code/severity 计数与编号；
- 不联网、不写文件、不做语义事实核查。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional, Tuple

from app.services.validators import (
    CitationValidator,
    LabelCitationValidator,
    ValidationReport,
)

#: 默认注册表；校验器都是无状态的纯函数对象，可安全复用。
DEFAULT_VALIDATORS: Dict[str, Any] = {
    "citation": CitationValidator(),
    "citation_labels": LabelCitationValidator(),
}

ValidatorRegistry = Mapping[str, Any]


def available_validators() -> Tuple[str, ...]:
    """返回默认可用的校验器名字（排序稳定）。"""
    return tuple(sorted(DEFAULT_VALIDATORS))


def run_validators(
    names: Any,
    *,
    answer: Any,
    citations: Any,
    registry: Optional[ValidatorRegistry] = None,
) -> Dict[str, Any]:
    """按声明顺序运行校验器，返回脱敏摘要；任何失败都不抛异常。"""
    table: ValidatorRegistry = DEFAULT_VALIDATORS if registry is None else registry
    wanted = _names(names)

    entries: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for name in wanted:
        if name in seen:
            continue
        seen.add(name)

        validator = table.get(name) if isinstance(table, Mapping) else None
        if validator is None:
            entries.append(_error_entry(name, "unknown_validator"))
            continue

        try:
            report = validator.validate({"answer": answer, "citations": citations})
        except Exception:  # noqa: BLE001 - 校验器异常不能影响技能结果
            entries.append(_error_entry(name, "validator_exception"))
            continue

        if not isinstance(report, ValidationReport):
            entries.append(_error_entry(name, "invalid_report"))
            continue

        entries.append(_report_entry(report, name))

    error_count = sum(entry["error_count"] for entry in entries)
    warning_count = sum(entry["warning_count"] for entry in entries)
    issue_codes = sorted(
        {code for entry in entries for code in entry.get("issue_codes", [])}
    )
    unknown_ids = sorted(
        {item for entry in entries for item in entry.get("unknown_ids", [])}
    )
    cited_count = sum(entry["cited_count"] for entry in entries)

    return {
        "ok": error_count == 0,
        "validators": entries,
        "issue_codes": issue_codes,
        "error_count": error_count,
        "warning_count": warning_count,
        "cited_count": cited_count,
        "unknown_ids": unknown_ids,
    }


def _names(value: Any) -> List[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw = list(value)
    else:
        raw = []
    result: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip())
    return result


def _report_entry(report: ValidationReport, declared_name: str) -> Dict[str, Any]:
    stats = report.stats if isinstance(report.stats, dict) else {}
    issue_codes = sorted(
        {
            issue.code
            for issue in report.issues
            if isinstance(getattr(issue, "code", None), str) and issue.code
        }
    )
    return {
        "name": str(getattr(report, "validator", "") or declared_name),
        "version": str(getattr(report, "version", "") or ""),
        "ok": bool(report.ok),
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "issue_codes": issue_codes,
        "cited_count": _count(stats.get("cited_count")),
        "unknown_ids": _ids(stats.get("unknown_ids")),
    }


def _error_entry(name: str, code: str) -> Dict[str, Any]:
    return {
        "name": name,
        "version": "",
        "ok": False,
        "error": code,
        "error_count": 1,
        "warning_count": 0,
        "issue_codes": [code],
        "cited_count": 0,
        "unknown_ids": [],
    }


def _count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _ids(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return sorted(
        {item for item in value if isinstance(item, str) and item}
    )


__all__ = [
    "DEFAULT_VALIDATORS",
    "ValidatorRegistry",
    "available_validators",
    "run_validators",
]
