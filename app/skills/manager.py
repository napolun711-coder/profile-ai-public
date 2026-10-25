"""P2A：Skill Manager —— 选择、执行、预算、trace、一层回退。

边界：
- **不接生产**：没有 HTTP 路由、没有真实技能、没有网络调用；
- 技能异常/坏输出都在这里收敛成 `SkillOutput(status="error")`，不向调用方抛；
- 技能返回的 `trace` 会被 Manager 覆盖为脱敏轨迹；技能自己的过程信息只能写进
  `ctx.trace.detail` 的计数/枚举字段；
- 只有 `status="error"` 触发一次 fallback；`partial` / `refused` 不触发；
- fallback 不再递归，fallback 自己失败就返回它的错误。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from app.skills.contracts import (
    ERROR_FALLBACK_MISSING,
    ERROR_FORCE_MISSING,
    ERROR_INVALID_INPUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_SKILL,
    ERROR_SKILL_EXCEPTION,
    Skill,
    SkillError,
    SkillInput,
    SkillOutput,
    validate_skill_output,
)
from app.skills.registry import SkillNotFoundError, SkillRegistry
from app.skills.router import RoutingDecision, SkillRouter
from app.skills.runtime import SkillContext, SkillTrace
from app.skills.validation import run_validators

logger = logging.getLogger(__name__)


class SkillManager:
    """对注册表 + 路由 + ToolManager 的一次包装；所有跨边界调用从这里过。"""

    def __init__(
        self,
        registry: SkillRegistry,
        tools: Optional[Any] = None,
        *,
        router: Optional[SkillRouter] = None,
        fallback: str = "",
        logger_: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if not isinstance(registry, SkillRegistry):
            raise TypeError("SkillManager 需要 SkillRegistry")
        if router is not None and not isinstance(router, SkillRouter):
            raise TypeError("router 必须是 SkillRouter 或 None")
        if not isinstance(fallback, str):
            raise ValueError("fallback 必须是字符串")

        self.registry = registry
        self.tools = tools
        self.router = router or SkillRouter(registry, fallback=fallback)
        # 路由没配 fallback、Manager 配了时，以 Manager 的为准；两者一致是常态。
        self.fallback = fallback or self.router.fallback
        self._logger = logger_ or logger
        self._clock = clock

    # ============================================================
    # 对外入口
    # ============================================================

    def run(
        self,
        request: SkillInput,
        *,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> SkillOutput:
        """执行一次技能请求；任何失败都变成 `SkillOutput`，不抛异常。

        P3C：`emit` / `cancelled` 是流式回调，只供声明了流式能力的工具使用；
        不改变路由、预算、trace 与审计语义。
        """
        if not isinstance(request, SkillInput):
            return self._error(
                "none", "", ERROR_INVALID_INPUT, "request 必须是 SkillInput"
            )
        if not request.question.strip():
            return self._error("none", "", ERROR_INVALID_INPUT, "问题不能为空")

        options = request.options if isinstance(request.options, dict) else {}
        force = options.get("force_skill", "")
        if not isinstance(force, str):
            force = ""

        decision = self.router.select(request, force=force)
        if decision.skill is None:
            if decision.reason == "none" and self.fallback:
                try:
                    skill = self.registry.get(self.fallback)
                except SkillNotFoundError:
                    return self._error(
                        "fallback_missing",
                        "",
                        ERROR_FALLBACK_MISSING,
                        f"未注册的回退技能：{self.fallback}",
                    )
                decision = RoutingDecision(
                    skill, skill.spec.name, 0.0, "fallback"
                )
            else:
                code, message = self._missing_message(decision.reason, force)
                return self._error(decision.reason, "", code, message)

        output, trace = self._execute(
            decision.skill, request, decision, emit=emit, cancelled=cancelled
        )

        if (
            output.status == "error"
            and self.fallback
            and decision.name != self.fallback
        ):
            try:
                fallback_skill = self.registry.get(self.fallback)
            except SkillNotFoundError:
                fallback_skill = None

            if fallback_skill is None:
                trace.detail["fallback_missing"] = True
                output.trace = trace.to_dict()
                return output

            fallback_decision = RoutingDecision(
                fallback_skill,
                fallback_skill.spec.name,
                0.0,
                "fallback",
            )
            fallback_output, fallback_trace = self._execute(
                fallback_skill,
                request,
                fallback_decision,
                emit=emit,
                cancelled=cancelled,
            )
            fallback_trace.fallback_from = decision.name
            fallback_trace.detail["primary_error"] = {
                "code": output.error.code if output.error else ""
            }
            fallback_output.trace = fallback_trace.to_dict()
            return fallback_output

        return output

    # ============================================================
    # 执行
    # ============================================================

    def _execute(
        self,
        skill: Skill,
        request: SkillInput,
        decision: RoutingDecision,
        *,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[SkillOutput, SkillTrace]:
        spec = skill.spec
        trace = SkillTrace(
            route=decision.reason,
            confidence=float(decision.confidence),
            skill=spec.name,
        )
        ctx = SkillContext(
            skill=spec.name,
            spec=spec,
            tools=self.tools,
            request=request,
            trace=trace,
            clock=self._clock,
            emit=emit,
            cancelled=cancelled,
        )

        try:
            output = skill.execute(request, ctx)
        except Exception as exc:  # noqa: BLE001 - 技能边界必须收敛异常
            # 只留异常类型：str(exc) 可能带上上游正文。
            self._logger.warning(
                "技能 %s 执行异常：%s", spec.name, type(exc).__name__
            )
            return self._finish_error(
                trace,
                ctx,
                ERROR_SKILL_EXCEPTION,
                f"技能异常（{type(exc).__name__}）",
            )

        if not isinstance(output, SkillOutput):
            return self._finish_error(
                trace,
                ctx,
                ERROR_INVALID_OUTPUT,
                f"技能返回了非 SkillOutput 对象：{type(output).__name__}",
            )

        try:
            validate_skill_output(output)
        except ValueError as exc:
            return self._finish_error(
                trace,
                ctx,
                ERROR_INVALID_OUTPUT,
                f"技能输出不合法：{exc}",
            )

        # 先按技能执行本身计算软超时，再跑校验器：校验耗时不计入技能预算，
        # 也不会把 ok 降成 partial（P2D：校验只记录、不阻断）。
        elapsed_ms = ctx.elapsed_ms()
        over_timeout = elapsed_ms > float(spec.timeout_s) * 1000
        if over_timeout and output.status == "ok":
            output.status = "partial"

        trace.status = output.status
        trace.elapsed_ms = elapsed_ms
        trace.steps = ctx.steps
        trace.evidence_count = len(output.evidence)
        trace.citation_count = len(output.citations)
        trace.over_timeout = over_timeout
        if output.error is not None:
            trace.error = output.error.to_dict()

        if spec.validators:
            trace.detail["validation"] = _run_validation(output, spec.validators)

        # Manager 的轨迹是权威版本；技能自带的 trace 不向上传递。
        output.trace = trace.to_dict()
        return output, trace

    def _finish_error(
        self,
        trace: SkillTrace,
        ctx: SkillContext,
        code: str,
        message: str,
    ) -> Tuple[SkillOutput, SkillTrace]:
        elapsed_ms = ctx.elapsed_ms()
        trace.status = "error"
        trace.elapsed_ms = elapsed_ms
        trace.steps = ctx.steps
        trace.over_timeout = elapsed_ms > float(ctx.spec.timeout_s) * 1000
        trace.error = {"code": code, "message": message}

        output = SkillOutput(
            status="error",
            error=SkillError(code, message),
            trace=trace.to_dict(),
        )
        return output, trace

    # ============================================================
    # 错误
    # ============================================================

    def _error(
        self,
        route: str,
        skill: str,
        code: str,
        message: str,
    ) -> SkillOutput:
        trace = SkillTrace(
            route=route,
            skill=skill,
            status="error",
            error={"code": code, "message": message},
        )
        return SkillOutput(
            status="error",
            error=SkillError(code, message),
            trace=trace.to_dict(),
        )

    def _missing_message(self, reason: str, force: str) -> Tuple[str, str]:
        if reason == "force_missing":
            return ERROR_FORCE_MISSING, f"未注册的技能：{force or '（空）'}"
        if reason == "fallback_missing":
            return ERROR_FALLBACK_MISSING, f"未注册的回退技能：{self.router.fallback}"
        return ERROR_NO_SKILL, "没有匹配的技能"


def _run_validation(output: SkillOutput, names: Tuple[str, ...]) -> dict:
    """运行声明的校验器；任何意外都收敛成脱敏摘要，不影响技能结果。"""
    try:
        return run_validators(
            names,
            answer=output.answer,
            citations=output.citations,
        )
    except Exception:  # noqa: BLE001 - 校验是旁路，绝不允许反噬主流程
        return {
            "ok": False,
            "validators": [],
            "issue_codes": ["validator_exception"],
            "error_count": 1,
            "warning_count": 0,
            "cited_count": 0,
            "unknown_ids": [],
        }


__all__ = ["SkillManager"]
