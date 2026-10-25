"""P2A：Skill 契约（只声明，不执行真实业务）。

设计约束：
1. 这是**加法**：`app/skills` 不被任何现有路由/服务导入，P2A 只给测试与后续影子脚本使用；
2. 契约只描述“技能是什么、允许调用哪些工具、预算多少”，运行时的选择/预算/trace
   在 `runtime.py`、`router.py`、`manager.py`；
3. 技能名是单段小写标识（`knowledge_qa`），工具名仍是点分标识（`knowledge.search`）；
4. 输出里的 `trace` 由 Manager 覆盖，技能自己的过程信息只能写进
   `ctx.trace.detail` 的计数/枚举字段，不能放问题、答案、证据正文或参数值。
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from app.plugins.contracts import TOOL_NAME_RE, Evidence

if TYPE_CHECKING:  # 只用于类型标注，运行时不导入，避免循环与生产模块耦合
    from app.services.context_builder import Citation
    from app.skills.runtime import SkillContext

#: 技能名：单段小写标识（不允许点分、大写、空格）；与工具名刻意区分。
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 技能输出状态：ok=完成；partial=有结果但预算/证据不完整；refused=规则拒绝；error=失败。
SKILL_STATUSES: Tuple[str, ...] = ("ok", "partial", "refused", "error")

#: 运行时错误码（与 ToolError 的错误码并列，UI/脚本可按这些值分支）。
ERROR_NO_SKILL = "no_skill"
ERROR_FORCE_MISSING = "force_missing"
ERROR_FALLBACK_MISSING = "fallback_missing"
ERROR_INVALID_INPUT = "invalid_input"
ERROR_INVALID_OUTPUT = "invalid_output"
ERROR_SKILL_EXCEPTION = "skill_exception"
ERROR_BUDGET_EXCEEDED = "budget_exceeded"
ERROR_SKILL_TIMEOUT = "skill_timeout"
ERROR_PERMISSION = "permission_denied"
ERROR_INTERNAL = "internal_error"


@dataclass(frozen=True)
class SkillSpec:
    """技能的静态说明；由 `validate_skill_spec()` 校验，不在这里做隐式检查。"""

    name: str
    version: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    required_tools: Tuple[str, ...] = ()
    validators: Tuple[str, ...] = ()
    max_steps: int = 4
    timeout_s: float = 180.0
    enabled_by_default: bool = True
    keywords: Tuple[str, ...] = ()
    priority: int = 0


@dataclass(frozen=True)
class SkillInput:
    """一次技能请求；`options` 只放调用方显式给出的选项。

    `force_skill` 是唯一的调试/测试专用选项，由 Manager 读取；其他键由具体技能解释。
    """

    question: str
    options: Dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.question, str):
            raise ValueError("SkillInput.question 必须是字符串")
        if not isinstance(self.options, dict):
            raise ValueError("SkillInput.options 必须是对象")
        if not isinstance(self.trace_id, str):
            raise ValueError("SkillInput.trace_id 必须是字符串")


@dataclass(frozen=True)
class SkillError:
    """技能失败的结构化原因；`message` 可直接展示给用户，但不得包含正文。"""

    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("SkillError.code 不能为空")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("SkillError.message 不能为空")
        if not isinstance(self.retryable, bool):
            raise ValueError("SkillError.retryable 必须是布尔值")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": bool(self.retryable),
        }


@dataclass
class SkillOutput:
    """技能返回；`trace` 由 Manager 执行完后覆盖为脱敏轨迹。"""

    status: str = "ok"
    answer: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    citations: List["Citation"] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    trace: Dict[str, Any] = field(default_factory=dict)
    error: Optional[SkillError] = None

    def __post_init__(self) -> None:
        if self.status not in SKILL_STATUSES:
            raise ValueError(f"SkillOutput.status 必须是 {', '.join(SKILL_STATUSES)} 之一")
        if not isinstance(self.answer, str):
            raise ValueError("SkillOutput.answer 必须是字符串")
        if not isinstance(self.evidence, list):
            raise ValueError("SkillOutput.evidence 必须是列表")
        if not isinstance(self.citations, list):
            raise ValueError("SkillOutput.citations 必须是列表")
        if not isinstance(self.usage, dict):
            raise ValueError("SkillOutput.usage 必须是对象")
        if not isinstance(self.trace, dict):
            raise ValueError("SkillOutput.trace 必须是对象")
        if self.error is not None and not isinstance(self.error, SkillError):
            raise ValueError("SkillOutput.error 必须是 SkillError 或 None")

    def to_dict(self, *, include_answer: bool = False) -> Dict[str, Any]:
        """序列化；默认**不输出答案/证据/引用正文**，用于日志与追踪。"""
        payload: Dict[str, Any] = {
            "status": self.status,
            "usage": dict(self.usage),
            "trace": dict(self.trace),
            "evidence_count": len(self.evidence),
            "citation_count": len(self.citations),
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if include_answer:
            payload["answer"] = self.answer
            payload["evidence"] = [_safe_item(item) for item in self.evidence]
            payload["citations"] = [_safe_item(item) for item in self.citations]
        return payload


@runtime_checkable
class Skill(Protocol):
    """技能的最小协议：静态说明 + 规则评分 + 执行。"""

    spec: SkillSpec

    def can_handle(self, request: SkillInput) -> float:
        ...

    def execute(self, request: SkillInput, ctx: "SkillContext") -> SkillOutput:
        ...


def validate_skill_spec(spec: SkillSpec) -> None:
    """校验静态说明；不合法时抛 ValueError（注册表与单测共用）。"""
    if not isinstance(spec, SkillSpec):
        raise ValueError("validate_skill_spec 只接受 SkillSpec")

    label = spec.name if isinstance(spec.name, str) and spec.name else "?"

    def fail(reason: str) -> None:
        raise ValueError(f"SkillSpec({label}) 非法：{reason}")

    if not isinstance(spec.name, str) or not SKILL_NAME_RE.match(spec.name):
        fail("name 必须是单段小写标识（如 knowledge_qa）")
    if not isinstance(spec.version, str) or not spec.version.strip():
        fail("version 不能为空")
    if not isinstance(spec.description, str) or not spec.description.strip():
        fail("description 不能为空")
    if not isinstance(spec.input_schema, dict):
        fail("input_schema 必须是对象")
    if not isinstance(spec.output_schema, dict):
        fail("output_schema 必须是对象")
    if not isinstance(spec.required_tools, tuple) or not all(
        isinstance(item, str) and item for item in spec.required_tools
    ):
        fail("required_tools 必须是字符串元组（元素非空）")
    for tool_name in spec.required_tools:
        if not TOOL_NAME_RE.match(tool_name):
            fail(f"required_tools 中的工具名不合法：{tool_name}")
    if not isinstance(spec.validators, tuple) or not all(
        isinstance(item, str) and item for item in spec.validators
    ):
        fail("validators 必须是字符串元组（元素非空）")
    if isinstance(spec.max_steps, bool) or not isinstance(spec.max_steps, int):
        fail("max_steps 必须是整数")
    if spec.max_steps < 1:
        fail("max_steps 至少为 1")
    if isinstance(spec.timeout_s, bool) or not isinstance(spec.timeout_s, (int, float)):
        fail("timeout_s 必须是数字")
    if not math.isfinite(float(spec.timeout_s)) or float(spec.timeout_s) <= 0:
        fail("timeout_s 必须是大于 0 的有限数字")
    if not isinstance(spec.enabled_by_default, bool):
        fail("enabled_by_default 必须是布尔值")
    if not isinstance(spec.keywords, tuple) or not all(
        isinstance(item, str) and item for item in spec.keywords
    ):
        fail("keywords 必须是字符串元组（元素非空）")
    if isinstance(spec.priority, bool) or not isinstance(spec.priority, int):
        fail("priority 必须是整数")


def validate_skill_output(output: SkillOutput) -> None:
    """校验技能输出的一致性；不合法时抛 ValueError（Manager 会转成 invalid_output）。"""
    if not isinstance(output, SkillOutput):
        raise ValueError("validate_skill_output 只接受 SkillOutput")
    if output.status not in SKILL_STATUSES:
        raise ValueError(f"status 必须是 {', '.join(SKILL_STATUSES)} 之一")
    if not isinstance(output.answer, str):
        raise ValueError("answer 必须是字符串")
    if not isinstance(output.evidence, list):
        raise ValueError("evidence 必须是列表")
    if not isinstance(output.citations, list):
        raise ValueError("citations 必须是列表")
    if not isinstance(output.usage, dict):
        raise ValueError("usage 必须是对象")
    if not isinstance(output.trace, dict):
        raise ValueError("trace 必须是对象")
    if output.status == "error" and output.error is None:
        raise ValueError("error 状态必须带 SkillError")
    if output.status != "error" and output.error is not None:
        raise ValueError("非 error 状态不能带 SkillError")


def _safe_item(item: Any) -> Dict[str, Any]:
    """把 Evidence/Citation 转成字典；未知对象只留类型名，避免意外带出正文。"""
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    return {"type": type(item).__name__}


__all__ = [
    "ERROR_BUDGET_EXCEEDED",
    "ERROR_FALLBACK_MISSING",
    "ERROR_FORCE_MISSING",
    "ERROR_INTERNAL",
    "ERROR_INVALID_INPUT",
    "ERROR_INVALID_OUTPUT",
    "ERROR_NO_SKILL",
    "ERROR_PERMISSION",
    "ERROR_SKILL_EXCEPTION",
    "ERROR_SKILL_TIMEOUT",
    "SKILL_NAME_RE",
    "SKILL_STATUSES",
    "Skill",
    "SkillError",
    "SkillInput",
    "SkillOutput",
    "SkillSpec",
    "validate_skill_output",
    "validate_skill_spec",
]
