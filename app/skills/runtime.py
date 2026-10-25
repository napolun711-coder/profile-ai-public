"""P2A：Skill 运行时 —— 预算、trace、工具入口。

关键边界：
- 技能只能通过 `SkillContext.call_tool()` 调工具；这里先按 `required_tools` 做一次
  权限检查，再交给 `ToolManager`（它还会按全局策略、外部开关、side_effect 再查一遍）；
- `max_steps` 统计**每一次尝试**，包括被拒绝的调用；
- `timeout_s` 是软超时：调用前过期直接返回 `skill_timeout`，调用后超时由 Manager
  把 `ok` 降为 `partial`；这里不杀线程；
- trace 只记工具名、结果码、耗时与参数**键名**，不记参数值、问题、答案或证据正文。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.plugins.contracts import ToolContext, ToolError, ToolResult
from app.skills.contracts import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_PERMISSION,
    ERROR_SKILL_TIMEOUT,
    SkillInput,
    SkillSpec,
)


@dataclass
class SkillTrace:
    """一次技能执行的脱敏轨迹；字段固定，便于脚本/测试稳定比对。"""

    route: str = ""
    confidence: float = 0.0
    skill: str = ""
    status: str = ""
    elapsed_ms: float = 0.0
    steps: int = 0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    evidence_count: int = 0
    citation_count: int = 0
    over_timeout: bool = False
    fallback_from: str = ""
    error: Dict[str, str] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "confidence": round(float(self.confidence), 3),
            "skill": self.skill,
            "status": self.status,
            "elapsed_ms": round(float(self.elapsed_ms), 1),
            "steps": int(self.steps),
            "tool_calls": [dict(item) for item in self.tool_calls],
            "evidence_count": int(self.evidence_count),
            "citation_count": int(self.citation_count),
            "over_timeout": bool(self.over_timeout),
            "fallback_from": self.fallback_from,
            "error": dict(self.error),
            "detail": dict(self.detail),
        }


class SkillContext:
    """传给 `Skill.execute()` 的唯一运行时入口。"""

    def __init__(
        self,
        *,
        skill: str,
        spec: SkillSpec,
        tools: Optional[Any],
        request: SkillInput,
        trace: SkillTrace,
        clock: Callable[[], float] = time.perf_counter,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.skill = str(skill)
        self.spec = spec
        self.tools = tools
        self.request = request
        self.trace = trace
        # P3C：只用于流式技能/工具；普通技能不读。
        self.emit = emit
        self.cancelled = cancelled
        self.allowed_tools: Tuple[str, ...] = tuple(spec.required_tools)
        self.max_steps = int(spec.max_steps)
        self.timeout_s = float(spec.timeout_s)
        self.steps = 0
        self._clock = clock
        self.started = float(clock())
        self.deadline = self.started + self.timeout_s

    # ============================================================
    # 预算
    # ============================================================

    def remaining_s(self) -> float:
        """距软超时的剩余秒数；已过期返回 0。"""
        return max(0.0, self.deadline - float(self._clock()))

    def expired(self) -> bool:
        return float(self._clock()) >= self.deadline

    def elapsed_ms(self) -> float:
        return max(0.0, (float(self._clock()) - self.started) * 1000)

    # ============================================================
    # 工具入口
    # ============================================================

    def call_tool(
        self,
        name: str,
        *,
        args: Optional[Dict[str, Any]] = None,
        tool_ctx: Optional[ToolContext] = None,
    ) -> ToolResult:
        """调用一个已声明工具；任何失败都返回 `ToolResult.failure`，不抛异常。"""
        started = float(self._clock())
        raw_args: Any = {} if args is None else args
        args_keys = sorted(raw_args) if isinstance(raw_args, dict) else []

        self.steps += 1
        entry: Dict[str, Any] = {
            "tool": name if isinstance(name, str) else str(name),
            "ok": False,
            "code": "",
            "elapsed_ms": 0.0,
            "args_keys": args_keys,
        }

        if self.steps > self.max_steps:
            result = _failure(
                ERROR_BUDGET_EXCEEDED,
                f"技能调用步数超过预算（最多 {self.max_steps} 步）",
            )
            entry["code"] = ERROR_BUDGET_EXCEEDED
            self.trace.tool_calls.append(entry)
            return result

        if not isinstance(name, str) or not name.strip():
            result = _failure(ERROR_INVALID_INPUT, "工具名必须是非空字符串")
            entry["code"] = ERROR_INVALID_INPUT
            self.trace.tool_calls.append(entry)
            return result

        if name not in self.allowed_tools:
            result = _failure(
                ERROR_PERMISSION,
                "技能未声明使用该工具",
                detail="undeclared",
            )
            entry["code"] = ERROR_PERMISSION
            self.trace.tool_calls.append(entry)
            return result

        if float(self._clock()) >= self.deadline:
            result = _failure(
                ERROR_SKILL_TIMEOUT,
                f"技能调用超过 {self.timeout_s:.0f} 秒，已放弃等待",
                retryable=True,
            )
            entry["code"] = ERROR_SKILL_TIMEOUT
            self.trace.tool_calls.append(entry)
            return result

        if self.tools is None:
            result = _failure(ERROR_INTERNAL, "未配置 ToolManager，无法调用工具")
            entry["code"] = ERROR_INTERNAL
            entry["elapsed_ms"] = _elapsed_ms(started, self._clock())
            self.trace.tool_calls.append(entry)
            return result

        try:
            result = self.tools.call(
                name,
                args=raw_args,
                ctx=tool_ctx,
                caller="skill",
                declared=self.allowed_tools,
            )
        except Exception as exc:  # noqa: BLE001 - 技能边界必须把异常变成可展示原因
            # 只留异常类型：str(exc) 可能带上上游正文。
            result = _failure(
                ERROR_INTERNAL,
                f"工具调用异常（{type(exc).__name__}）",
            )

        entry["ok"] = bool(result.ok)
        entry["code"] = result.error.code if result.error else ""
        entry["elapsed_ms"] = _elapsed_ms(started, self._clock())
        self.trace.tool_calls.append(entry)
        return result


def _failure(code: str, message: str, *, detail: str = "", retryable: bool = False) -> ToolResult:
    return ToolResult.failure(
        ToolError(code=code, message=message, detail=detail, retryable=retryable)
    )


def _elapsed_ms(started: float, finished: float) -> float:
    return round(max(0.0, (float(finished) - float(started)) * 1000), 1)


__all__ = ["SkillContext", "SkillTrace"]
