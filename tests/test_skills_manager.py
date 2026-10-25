"""P2A：SkillManager 的离线单测（假技能 + 真实 ToolManager + 临时审计目录）。

覆盖：选择/强制、空问题、异常收敛、坏输出、工具权限、外部策略、步数预算、
软超时、一层回退、trace 脱敏。全程不联网、不碰真实 data/ 与 logs/。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.plugins import (
    AuditLogger,
    Evidence,
    Health,
    RetryPolicy,
    ToolManager,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from app.services.context_builder import Citation
from app.skills import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_FORCE_MISSING,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_SKILL,
    ERROR_PERMISSION,
    ERROR_SKILL_EXCEPTION,
    ERROR_SKILL_TIMEOUT,
    SkillError,
    SkillInput,
    SkillManager,
    SkillOutput,
    SkillRegistry,
    SkillSpec,
)


def make_tool_spec(
    name: str = "demo.echo",
    *,
    external: bool = False,
    side_effect: str = "none",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="0.1.0",
        description="示例工具",
        capabilities=("echo",),
        side_effect=side_effect,
        external=external,
        risk="low",
        timeout_s=5.0,
        retry=RetryPolicy(),
        allowed_for_skills=True,
        config_schema={},
    )


class FakeTool:
    def __init__(
        self,
        name: str = "demo.echo",
        *,
        spec: ToolSpec | None = None,
        result_factory=None,
    ) -> None:
        self.spec = spec or make_tool_spec(name)
        self.calls = []
        self._factory = result_factory

    def invoke(self, ctx=None, **kwargs):
        self.calls.append(kwargs)
        if self._factory is not None:
            return self._factory(len(self.calls), kwargs)
        return ToolResult.success({"echo": kwargs})

    def health(self, ctx=None):
        return Health(ok=True)


def make_skill_spec(
    name: str = "demo_skill",
    *,
    required_tools=("demo.echo",),
    max_steps: int = 4,
    timeout_s: float = 180.0,
    enabled: bool = True,
    validators=(),
) -> SkillSpec:
    return SkillSpec(
        name=name,
        version="0.1.0",
        description="示例技能",
        required_tools=tuple(required_tools),
        validators=tuple(validators),
        max_steps=max_steps,
        timeout_s=timeout_s,
        enabled_by_default=enabled,
    )


class FakeSkill:
    def __init__(
        self,
        name: str = "demo_skill",
        *,
        confidence: float = 1.0,
        spec: SkillSpec | None = None,
        output: SkillOutput | None = None,
        execute_fn=None,
    ) -> None:
        self.spec = spec or make_skill_spec(name)
        self._confidence = confidence
        self._output = output
        self._execute_fn = execute_fn
        self.executions = 0

    def can_handle(self, request: SkillInput) -> float:
        return self._confidence

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        self.executions += 1
        if self._execute_fn is not None:
            return self._execute_fn(request, ctx)
        if self._output is not None:
            return self._output
        return SkillOutput(answer="默认答案")


class ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.audit = AuditLogger(self.root / "audit")
        self.now = [100.0]
        self.tool_registry = ToolRegistry()
        self.tool_manager = ToolManager(
            self.tool_registry,
            audit=self.audit,
            clock=lambda: self.now[0],
            sleep=lambda seconds: None,
        )
        self.skill_registry = SkillRegistry()
        self.manager = SkillManager(
            self.skill_registry,
            tools=self.tool_manager,
            clock=lambda: self.now[0],
        )

    def tearDown(self) -> None:
        self.tool_manager.close()
        self._tmp.cleanup()

    def add_tool(self, tool):
        self.tool_registry.register(tool)
        return tool

    def add_skill(self, skill):
        self.skill_registry.register(skill)
        return skill


class SelectionTests(ManagerFixture):
    def test_successful_run_fills_trace(self) -> None:
        skill = self.add_skill(
            FakeSkill(
                output=SkillOutput(
                    answer="答案 [1]",
                    evidence=[Evidence(evidence_id="c1", kind="chunk")],
                    citations=[
                        Citation(
                            citation_id="c1",
                            label="[1]",
                            kind="chunk",
                            reference_id="1",
                        )
                    ],
                    usage={"tokens": 3},
                )
            )
        )

        result = self.manager.run(SkillInput("问题", trace_id="t1"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "答案 [1]")
        self.assertEqual(skill.executions, 1)

        trace = result.trace
        self.assertEqual(trace["route"], "rule")
        self.assertEqual(trace["skill"], "demo_skill")
        self.assertEqual(trace["confidence"], 1.0)
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["steps"], 0)
        self.assertEqual(trace["evidence_count"], 1)
        self.assertEqual(trace["citation_count"], 1)
        self.assertEqual(trace["tool_calls"], [])
        self.assertFalse(trace["over_timeout"])
        self.assertEqual(trace["error"], {})

    def test_empty_question_returns_error(self) -> None:
        skill = self.add_skill(FakeSkill())

        result = self.manager.run(SkillInput("   "))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
        self.assertEqual(result.trace["route"], "none")
        self.assertEqual(skill.executions, 0)

    def test_invalid_request_type_returns_error(self) -> None:
        result = self.manager.run({"question": "x"})  # type: ignore[arg-type]

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)

    def test_force_missing_skill(self) -> None:
        result = self.manager.run(
            SkillInput("问题", options={"force_skill": "missing_skill"})
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_FORCE_MISSING)
        self.assertEqual(result.trace["route"], "force_missing")

    def test_force_selects_low_confidence_skill(self) -> None:
        skill = self.add_skill(
            FakeSkill("forced_skill", confidence=0.0, spec=make_skill_spec("forced_skill", enabled=False))
        )

        result = self.manager.run(
            SkillInput("问题", options={"force_skill": "forced_skill"})
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.trace["route"], "force")
        self.assertEqual(result.trace["skill"], "forced_skill")
        self.assertEqual(skill.executions, 1)

    def test_no_matching_skill(self) -> None:
        self.add_skill(FakeSkill("low_skill", confidence=0.1))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_NO_SKILL)
        self.assertEqual(result.trace["route"], "none")


class ErrorTests(ManagerFixture):
    def test_skill_exception_is_sanitized(self) -> None:
        secret = "异常里的秘密正文"

        def explode(request, ctx):
            raise RuntimeError(secret)

        self.add_skill(FakeSkill(execute_fn=explode))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_SKILL_EXCEPTION)
        self.assertIn("RuntimeError", result.error.message)
        self.assertNotIn(secret, result.error.message)
        self.assertNotIn(secret, json.dumps(result.trace, ensure_ascii=False))

    def test_non_skill_output_becomes_invalid_output(self) -> None:
        self.add_skill(FakeSkill(execute_fn=lambda request, ctx: {"answer": "x"}))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_INVALID_OUTPUT)
        self.assertIn("dict", result.error.message)

    def test_error_status_without_error_is_invalid(self) -> None:
        self.add_skill(FakeSkill(output=SkillOutput(status="error")))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_INVALID_OUTPUT)
        self.assertIn("SkillError", result.error.message)


class ToolTests(ManagerFixture):
    def test_allowed_tool_call_is_traced_without_args(self) -> None:
        tool = self.add_tool(FakeTool("demo.echo"))

        def execute(request, ctx):
            tool_result = ctx.call_tool("demo.echo", args={"text": "秘密参数"})
            self.assertTrue(tool_result.ok)
            return SkillOutput(answer="ok")

        self.add_skill(FakeSkill(execute_fn=execute))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(tool.calls, [{"text": "秘密参数"}])
        self.assertEqual(result.trace["steps"], 1)
        self.assertEqual(len(result.trace["tool_calls"]), 1)

        entry = result.trace["tool_calls"][0]
        self.assertEqual(entry["tool"], "demo.echo")
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["args_keys"], ["text"])

        rendered = json.dumps(result.trace, ensure_ascii=False)
        self.assertNotIn("秘密参数", rendered)

        audit_records = self.audit.read()
        self.assertEqual(len(audit_records), 1)
        self.assertNotIn("秘密参数", json.dumps(audit_records, ensure_ascii=False))

    def test_undeclared_tool_is_denied(self) -> None:
        echo = self.add_tool(FakeTool("demo.echo"))
        other = self.add_tool(FakeTool("demo.other"))

        def execute(request, ctx):
            tool_result = ctx.call_tool("demo.other", args={"x": 1})
            self.assertFalse(tool_result.ok)
            self.assertEqual(tool_result.error.code, ERROR_PERMISSION)
            self.assertEqual(tool_result.error.detail, "undeclared")
            return SkillOutput(answer="denied")

        self.add_skill(FakeSkill(execute_fn=execute))

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(echo.calls, [])
        self.assertEqual(other.calls, [])
        self.assertEqual(result.trace["tool_calls"][0]["code"], ERROR_PERMISSION)

    def test_external_tool_requires_global_policy(self) -> None:
        search = self.add_tool(
            FakeTool(
                "knowledge.search",
                spec=make_tool_spec("knowledge.search", external=True),
            )
        )

        def execute(request, ctx):
            tool_result = ctx.call_tool(
                "knowledge.search", args={"query": "秘密查询"}
            )
            self.assertFalse(tool_result.ok)
            self.assertEqual(tool_result.error.code, ERROR_PERMISSION)
            self.assertEqual(tool_result.error.detail, "external_disabled")
            return SkillOutput(answer="denied")

        self.add_skill(
            FakeSkill(
                spec=make_skill_spec(
                    "demo_skill", required_tools=("knowledge.search",)
                ),
                execute_fn=execute,
            )
        )

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(search.calls, [])
        self.assertEqual(result.trace["tool_calls"][0]["code"], ERROR_PERMISSION)
        self.assertNotIn("秘密查询", json.dumps(result.trace, ensure_ascii=False))

    def test_max_steps_is_enforced(self) -> None:
        tool = self.add_tool(FakeTool("demo.echo"))

        def execute(request, ctx):
            results = [
                ctx.call_tool("demo.echo", args={"n": index}) for index in range(3)
            ]
            self.assertEqual([item.ok for item in results], [True, True, False])
            self.assertEqual(results[2].error.code, ERROR_BUDGET_EXCEEDED)
            return SkillOutput(answer="done")

        self.add_skill(
            FakeSkill(
                spec=make_skill_spec("demo_skill", max_steps=2),
                execute_fn=execute,
            )
        )

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(tool.calls), 2)
        self.assertEqual(result.trace["steps"], 3)
        self.assertEqual(len(result.trace["tool_calls"]), 3)
        self.assertEqual(
            result.trace["tool_calls"][-1]["code"], ERROR_BUDGET_EXCEEDED
        )

    def test_missing_tool_manager_is_reported(self) -> None:
        manager = SkillManager(
            self.skill_registry, tools=None, clock=lambda: self.now[0]
        )

        def execute(request, ctx):
            tool_result = ctx.call_tool("demo.echo")
            self.assertEqual(tool_result.error.code, ERROR_INTERNAL)
            return SkillOutput(answer="no tools")

        self.add_skill(FakeSkill(execute_fn=execute))

        result = manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.trace["tool_calls"][0]["code"], ERROR_INTERNAL)


class TimeoutTests(ManagerFixture):
    def test_timeout_before_call_returns_skill_timeout(self) -> None:
        tool = self.add_tool(FakeTool("demo.echo"))

        def execute(request, ctx):
            self.now[0] += 2.0
            tool_result = ctx.call_tool("demo.echo", args={"q": "x"})
            return SkillOutput(
                status="error",
                error=SkillError(tool_result.error.code, "失败"),
            )

        self.add_skill(
            FakeSkill(
                spec=make_skill_spec("demo_skill", timeout_s=1.0),
                execute_fn=execute,
            )
        )

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, ERROR_SKILL_TIMEOUT)
        self.assertEqual(tool.calls, [])
        self.assertEqual(result.trace["tool_calls"][0]["code"], ERROR_SKILL_TIMEOUT)
        self.assertTrue(result.trace["over_timeout"])

    def test_over_timeout_marks_partial(self) -> None:
        def execute(request, ctx):
            self.now[0] += 2.0
            return SkillOutput(answer="late")

        self.add_skill(
            FakeSkill(
                spec=make_skill_spec("demo_skill", timeout_s=1.0),
                execute_fn=execute,
            )
        )

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.trace["status"], "partial")
        self.assertTrue(result.trace["over_timeout"])
        self.assertEqual(result.trace["error"], {})


class FallbackTests(ManagerFixture):
    def manager_with_fallback(self) -> SkillManager:
        return SkillManager(
            self.skill_registry,
            tools=self.tool_manager,
            fallback="fallback_skill",
            clock=lambda: self.now[0],
        )

    def test_fallback_on_error(self) -> None:
        primary = self.add_skill(
            FakeSkill(
                "primary_skill",
                output=SkillOutput(
                    status="error", error=SkillError("boom", "主技能失败")
                ),
            )
        )
        fallback = self.add_skill(
            FakeSkill(
                "fallback_skill",
                confidence=0.0,
                output=SkillOutput(answer="兜底"),
            )
        )

        result = self.manager_with_fallback().run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.answer, "兜底")
        self.assertEqual(result.trace["route"], "fallback")
        self.assertEqual(result.trace["skill"], "fallback_skill")
        self.assertEqual(result.trace["fallback_from"], "primary_skill")
        self.assertEqual(
            result.trace["detail"]["primary_error"]["code"], "boom"
        )
        self.assertEqual(primary.executions, 1)
        self.assertEqual(fallback.executions, 1)

    def test_fallback_is_not_recursive(self) -> None:
        self.add_skill(
            FakeSkill(
                "primary_skill",
                output=SkillOutput(
                    status="error", error=SkillError("primary_boom", "主技能失败")
                ),
            )
        )
        fallback = self.add_skill(
            FakeSkill(
                "fallback_skill",
                confidence=0.0,
                output=SkillOutput(
                    status="error",
                    error=SkillError("fallback_boom", "兜底也失败"),
                ),
            )
        )

        result = self.manager_with_fallback().run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, "fallback_boom")
        self.assertEqual(result.trace["fallback_from"], "primary_skill")
        self.assertEqual(fallback.executions, 1)

    def test_partial_does_not_trigger_fallback(self) -> None:
        self.add_skill(
            FakeSkill(
                "primary_skill",
                output=SkillOutput(status="partial", answer="部分"),
            )
        )
        fallback = self.add_skill(
            FakeSkill(
                "fallback_skill",
                confidence=0.0,
                output=SkillOutput(answer="兜底"),
            )
        )

        result = self.manager_with_fallback().run(SkillInput("问题"))

        self.assertEqual(result.status, "partial")
        self.assertEqual(fallback.executions, 0)

    def test_fallback_when_no_primary_matches(self) -> None:
        low = self.add_skill(FakeSkill("low_skill", confidence=0.1))
        fallback = self.add_skill(
            FakeSkill("fallback_skill", confidence=0.0, output=SkillOutput(answer="兜底"))
        )

        result = self.manager_with_fallback().run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.trace["route"], "fallback")
        self.assertEqual(result.trace["skill"], "fallback_skill")
        self.assertEqual(low.executions, 0)
        self.assertEqual(fallback.executions, 1)

    def test_missing_fallback_after_primary_error(self) -> None:
        self.add_skill(
            FakeSkill(
                "primary_skill",
                output=SkillOutput(
                    status="error", error=SkillError("boom", "主技能失败")
                ),
            )
        )

        result = self.manager_with_fallback().run(SkillInput("问题"))

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error.code, "boom")
        self.assertTrue(result.trace["detail"]["fallback_missing"])


class ValidationTests(ManagerFixture):
    def test_declared_validator_is_recorded_without_changing_status(self) -> None:
        self.add_skill(
            FakeSkill(
                spec=make_skill_spec("demo_skill", validators=("citation",)),
                output=SkillOutput(
                    answer="答案 [9]",
                    citations=[
                        Citation(
                            citation_id="c1",
                            label="[1]",
                            kind="reference",
                            reference_id="1",
                        )
                    ],
                ),
            )
        )

        result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        validation = result.trace["detail"]["validation"]
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["error_count"], 1)
        self.assertEqual(validation["unknown_ids"], ["9"])

    def test_no_validators_does_not_add_validation_detail(self) -> None:
        self.add_skill(FakeSkill(output=SkillOutput(answer="没有引用")))

        result = self.manager.run(SkillInput("问题"))

        self.assertNotIn("validation", result.trace["detail"])

    def test_validator_exception_does_not_change_status(self) -> None:
        self.add_skill(
            FakeSkill(
                spec=make_skill_spec("demo_skill", validators=("citation",)),
                output=SkillOutput(answer="答案 [1]"),
            )
        )

        with mock.patch(
            "app.skills.manager.run_validators",
            side_effect=RuntimeError("校验器异常"),
        ):
            result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        validation = result.trace["detail"]["validation"]
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["issue_codes"], ["validator_exception"])

    def test_validator_time_is_not_counted_as_timeout(self) -> None:
        self.add_skill(
            FakeSkill(
                spec=make_skill_spec(
                    "demo_skill", timeout_s=1.0, validators=("citation",)
                ),
                output=SkillOutput(answer="答案 [1]"),
            )
        )

        def slow_validators(*args, **kwargs):
            self.now[0] += 2.0
            return {
                "ok": True,
                "validators": [],
                "issue_codes": [],
                "error_count": 0,
                "warning_count": 0,
                "cited_count": 1,
                "unknown_ids": [],
            }

        with mock.patch(
            "app.skills.manager.run_validators", side_effect=slow_validators
        ):
            result = self.manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.trace["over_timeout"])


class TraceRedactionTests(ManagerFixture):
    def test_trace_never_contains_question_answer_args_or_bodies(self) -> None:
        secrets = {
            "question": "机密问题正文",
            "answer": "机密答案正文",
            "evidence": "机密证据正文",
            "arg": "机密参数值",
            "caption": "机密图注",
        }
        self.add_tool(FakeTool("demo.echo"))

        def execute(request, ctx):
            ctx.call_tool("demo.echo", args={"query": secrets["arg"]})
            return SkillOutput(
                answer=secrets["answer"],
                evidence=[
                    Evidence(
                        evidence_id="c1",
                        kind="chunk",
                        text=secrets["evidence"],
                    )
                ],
                citations=[
                    Citation(
                        citation_id="c1",
                        label="[1]",
                        kind="image",
                        caption=secrets["caption"],
                    )
                ],
                trace={"secret": secrets["answer"]},
            )

        self.add_skill(FakeSkill(execute_fn=execute))

        result = self.manager.run(SkillInput(secrets["question"]))
        rendered = json.dumps(result.trace, ensure_ascii=False)

        for label, secret in secrets.items():
            with self.subTest(label=label):
                self.assertNotIn(secret, rendered)
        self.assertNotIn("secret", result.trace)


if __name__ == "__main__":
    unittest.main()
