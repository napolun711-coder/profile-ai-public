"""P1D：通用校验契约（纯数据结构，不接生产链路）。

边界：
    · Validator 只做**确定性的一致性检查**：输入 payload，输出 `ValidationReport`；
    · 不修改输入、不联网、不写文件、不评判事实正确性；
    · `ok = 没有 error`；warning 只提示，不使报告失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Protocol, runtime_checkable

SEVERITIES = ("error", "warning")


@dataclass(frozen=True)
class Issue:
    """一条校验结论；`ref` 只放编号/字段名，不放答案或上下文正文。"""

    code: str
    message: str
    severity: str = "warning"
    ref: str = ""

    def __post_init__(self) -> None:
        if not str(self.code or "").strip():
            raise ValueError("Issue.code 不能为空")
        if not str(self.message or "").strip():
            raise ValueError("Issue.message 不能为空")
        if self.severity not in SEVERITIES:
            raise ValueError(f"未知的 severity：{self.severity}")

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "ref": self.ref,
        }


@dataclass
class ValidationReport:
    """一次校验的完整结果；`stats` 只放计数与编号。"""

    validator: str
    version: str
    issues: List[Issue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def add(self, issue: Issue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> List[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> List[Issue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "validator": self.validator,
            "version": self.version,
            "issues": [issue.to_dict() for issue in self.issues],
            "stats": dict(self.stats),
        }


@runtime_checkable
class Validator(Protocol):
    """校验器接口；后续 quality/advice 等只读适配器也按这个形状接入。"""

    name: str
    version: str

    def validate(self, payload: Mapping[str, Any]) -> ValidationReport:
        ...
