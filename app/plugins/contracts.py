"""P0：统一 Tool 契约（只声明，不执行）。

设计约束：
1. 这是**加法**：现有 services / clients / routers 不依赖本模块；
2. 契约只描述“工具是什么”，Tool Manager 的运行时校验、超时、权限、审计留到 P1；
3. `external=True` 表示“可能把内容发到本机之外”，`side_effect` 表示是否改变状态；
4. 所有校验都是纯函数，单测可直接比对，不需要网络或临时文件。

字段口径（P0）：
- `ToolResult.data` 保持底层对象的原样：
  OCR → `OcrOutcome`；解析 → `ParseResult`；检索 → `/query/data` 原始字典。
- `ToolResult.error` 只在失败时出现；成功结果带 error 会直接报错，避免“半成功”状态。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

# 工具名：点分小写标识（如 ocr.recognize）；不允许空格、大写和单段名。
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

# 是否改变状态：none=只读且不落地；read_local=读本机文件；write_local/write_remote=会写。
SIDE_EFFECTS: Tuple[str, ...] = ("none", "read_local", "write_local", "write_remote")
RISKS: Tuple[str, ...] = ("low", "medium", "high")

# 统一的错误码：P1 的 Tool Manager 与 UI 直接按这些值分支，不再猜中文文案。
ERROR_INVALID_INPUT = "invalid_input"
ERROR_NOT_FOUND = "not_found"
ERROR_PARSE_FAILED = "parse_failed"
ERROR_OCR_FAILED = "ocr_failed"
ERROR_UPSTREAM = "upstream_error"
ERROR_INTERNAL = "internal_error"

# P1 Tool Manager 的运行时错误码（与适配器自己的错误码并列，UI 可直接分支）
ERROR_TIMEOUT = "timeout"                 # 硬超时（目前只用于只读外部工具）
ERROR_CIRCUIT_OPEN = "circuit_open"       # 熔断打开，本次未真正调用
ERROR_PERMISSION = "permission_denied"    # 策略不允许调用
ERROR_INVALID_OUTPUT = "invalid_output"   # 声明的 output_schema 校验未通过（软失败）

# 这些上游状态码可以安全重试（与 clients/lightrag.py 的 RETRY_STATUS_CODES 对齐）。
RETRYABLE_UPSTREAM_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """重试声明（P0 只声明，不执行；P1 的 Tool Manager 按它决定是否重试）。"""

    max_attempts: int = 1
    backoff_s: float = 0.0
    retry_on: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("RetryPolicy.max_attempts 必须是整数")
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts 至少为 1")
        if isinstance(self.backoff_s, bool) or not isinstance(self.backoff_s, (int, float)):
            raise ValueError("RetryPolicy.backoff_s 必须是数字")
        if self.backoff_s < 0:
            raise ValueError("RetryPolicy.backoff_s 不能为负数")
        if not isinstance(self.retry_on, tuple) or not all(
            isinstance(item, str) and item for item in self.retry_on
        ):
            raise ValueError("RetryPolicy.retry_on 必须是非空字符串元组")


@dataclass(frozen=True)
class ToolSpec:
    """工具的静态说明。P0 不执行它，只用于注册、列举与后续权限判断。"""

    name: str
    version: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    capabilities: Tuple[str, ...] = ()
    side_effect: str = "none"
    external: bool = False
    risk: str = "low"
    timeout_s: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    allowed_for_skills: bool = True
    config_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolError:
    """工具失败的结构化原因；中文 message 可直接展示给用户。"""

    code: str
    message: str
    retryable: bool = False
    detail: str = ""
    upstream_status: Optional[int] = None

    def __post_init__(self) -> None:
        if not (self.code or "").strip():
            raise ValueError("ToolError.code 不能为空")
        if not (self.message or "").strip():
            raise ValueError("ToolError.message 不能为空")

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": bool(self.retryable),
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.upstream_status is not None:
            payload["upstream_status"] = self.upstream_status
        return payload


@dataclass(frozen=True)
class Evidence:
    """检索/解析结果中的一条证据（P1 的 Context Builder 直接消费）。"""

    evidence_id: str
    kind: str
    source_id: str = ""
    doc_name: str = ""
    file_id: str = ""
    image_id: str = ""
    text: str = ""
    origin_url: str = ""
    score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (self.evidence_id or "").strip():
            raise ValueError("Evidence.evidence_id 不能为空")
        if not (self.kind or "").strip():
            raise ValueError("Evidence.kind 不能为空")


@dataclass
class ToolResult:
    """统一返回：成功带 data，失败带 error；两者不会同时出现。"""

    ok: bool
    data: Any = None
    error: Optional[ToolError] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Evidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError("成功的 ToolResult 不能带 error")
        if not self.ok and self.error is None:
            raise ValueError("失败的 ToolResult 必须带 error")
        if self.usage is None:
            self.usage = {}
        if self.evidence is None:
            self.evidence = []

    @classmethod
    def success(
        cls,
        data: Any = None,
        *,
        usage: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[Evidence]] = None,
    ) -> "ToolResult":
        return cls(
            ok=True,
            data=data,
            usage=dict(usage or {}),
            evidence=list(evidence or []),
        )

    @classmethod
    def failure(
        cls,
        error: ToolError,
        *,
        data: Any = None,
        usage: Optional[Dict[str, Any]] = None,
        evidence: Optional[List[Evidence]] = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            data=data,
            error=error,
            usage=dict(usage or {}),
            evidence=list(evidence or []),
        )


@dataclass(frozen=True)
class Health:
    """工具健康状态。`health()` 由调用方显式触发，导入/注册时不会探活。"""

    ok: bool
    detail: str = ""
    latency_ms: Optional[float] = None


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的上下文；不传用户身份/密钥。

    P3C：`emit` / `cancelled` 只供流式工具使用，是进程内回调，不序列化、不落盘；
    普通工具忽略这两个字段。事件回调只允许承载面向用户的内容（references/chunk）。
    """

    settings: Any = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("app.plugins"))
    trace_id: str = ""
    emit: Optional[Callable[[Dict[str, Any]], None]] = None
    cancelled: Optional[Callable[[], bool]] = None


@runtime_checkable
class Tool(Protocol):
    """工具的最小协议：静态说明 + 调用 + 健康检查。"""

    spec: ToolSpec

    def invoke(self, ctx: Optional[ToolContext] = None, **kwargs: Any) -> ToolResult:
        ...

    def health(self, ctx: Optional[ToolContext] = None) -> Health:
        ...


def validate_spec(spec: ToolSpec) -> None:
    """校验静态说明；不合法时抛 ValueError（注册表与单测共用）。"""

    if not isinstance(spec, ToolSpec):
        raise ValueError("validate_spec 只接受 ToolSpec")

    label = spec.name if isinstance(spec.name, str) and spec.name else "?"

    def fail(reason: str) -> None:
        raise ValueError(f"ToolSpec({label}) 非法：{reason}")

    if not isinstance(spec.name, str) or not TOOL_NAME_RE.match(spec.name):
        fail("name 必须是点分小写标识（如 ocr.recognize）")
    if not isinstance(spec.version, str) or not spec.version.strip():
        fail("version 不能为空")
    if not isinstance(spec.description, str) or not spec.description.strip():
        fail("description 不能为空")
    if not isinstance(spec.input_schema, dict):
        fail("input_schema 必须是对象")
    if not isinstance(spec.output_schema, dict):
        fail("output_schema 必须是对象")
    if not isinstance(spec.capabilities, tuple) or not all(
        isinstance(item, str) and item for item in spec.capabilities
    ):
        fail("capabilities 必须是非空字符串元组")
    if spec.side_effect not in SIDE_EFFECTS:
        fail(f"side_effect 必须是 {', '.join(SIDE_EFFECTS)} 之一")
    if not isinstance(spec.external, bool):
        fail("external 必须是布尔值")
    if spec.risk not in RISKS:
        fail(f"risk 必须是 {', '.join(RISKS)} 之一")
    if isinstance(spec.timeout_s, bool) or not isinstance(spec.timeout_s, (int, float)):
        fail("timeout_s 必须是数字")
    if float(spec.timeout_s) <= 0:
        fail("timeout_s 必须大于 0")
    if not isinstance(spec.retry, RetryPolicy):
        fail("retry 必须是 RetryPolicy")
    if not isinstance(spec.allowed_for_skills, bool):
        fail("allowed_for_skills 必须是布尔值")
    if not isinstance(spec.config_schema, dict):
        fail("config_schema 必须是对象")
