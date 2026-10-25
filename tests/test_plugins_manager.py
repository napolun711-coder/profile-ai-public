"""P1A：Tool Manager 运行时的离线单测。

覆盖：参数/结果校验、权限、路径白名单、重试、硬超时、熔断、审计脱敏。
全程不联网、不碰真实 data/ 与 logs/：审计写到临时目录，工具都是本地假对象。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.plugins import (
    ERROR_CIRCUIT_OPEN,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_TIMEOUT,
    AuditLogger,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolManager,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    check_path,
)

ROOT = Path(__file__).resolve().parents[1]


def make_spec(**overrides):
    values = {
        "name": "demo.echo",
        "version": "0.1.0",
        "description": "示例工具",
        "input_schema": {},
        "output_schema": {},
        "capabilities": ("echo",),
        "side_effect": "none",
        "external": False,
        "risk": "low",
        "timeout_s": 5.0,
        "retry": RetryPolicy(),
        "allowed_for_skills": True,
        "config_schema": {},
    }
    values.update(overrides)
    return ToolSpec(**values)


def failing(code: str = "upstream_error", *, retryable: bool = True) -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message="失败", retryable=retryable))


class FakeTool:
    def __init__(self, spec=None, result_factory=None) -> None:
        self.spec = spec or make_spec()
        self.calls = []
        self._factory = result_factory

    def invoke(self, ctx=None, **kwargs):
        self.calls.append(kwargs)
        if self._factory is not None:
            return self._factory(len(self.calls), kwargs)
        return ToolResult.success({"ok": True})

    def health(self, ctx=None):
        return Health(ok=True)


class ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.audit = AuditLogger(self.root / "audit")
        self.now = [100.0]
        self.sleeps = []
        self.manager = ToolManager(
            ToolRegistry(),
            audit=self.audit,
            clock=lambda: self.now[0],
            sleep=self.sleeps.append,
        )

    def tearDown(self) -> None:
        self.manager.close()
        self._tmp.cleanup()

    def add(self, tool):
        self.manager.registry.register(tool)
        return tool


class BasicCallTests(ManagerFixture):
    def test_success_usage_and_audit_has_no_values(self) -> None:
        tool = self.add(FakeTool())

        result = self.manager.call("demo.echo", args={"query": "秘密内容"})

        self.assertTrue(result.ok)
        self.assertEqual(result.usage["attempts"], 1)
        self.assertIn("manager_elapsed_ms", result.usage)
        self.assertEqual(tool.calls, [{"query": "秘密内容"}])

        records = self.audit.read()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["tool"], "demo.echo")
        self.assertEqual(record["caller"], "script")
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["error_code"], "")
        self.assertEqual(record["args_keys"], ["query"])
        self.assertEqual(len(record["args_sha256"]), 64)
        self.assertEqual(record["attempts"], 1)
        self.assertFalse(record["output_invalid"])

        raw = self.audit.path_for().read_text(encoding="utf-8")
        self.assertNotIn("秘密内容", raw)
        self.assertIn("args_sha256", raw)

    def test_unknown_tool_returns_failure_and_is_audited(self) -> None:
        result = self.manager.call("missing.tool")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_NOT_FOUND)
        self.assertEqual(result.usage["attempts"], 0)
        records = self.audit.read()
        self.assertEqual(records[0]["tool"], "missing.tool")
        self.assertEqual(records[0]["error_code"], ERROR_NOT_FOUND)

    def test_non_dict_args_rejected(self) -> None:
        tool = self.add(FakeTool())

        result = self.manager.call("demo.echo", args=["nope"])  # type: ignore[arg-type]

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
        self.assertEqual(tool.calls, [])
        self.assertEqual(self.audit.read()[0]["args_keys"], [])

    def test_input_schema_failure_does_not_invoke(self) -> None:
        spec = make_spec(
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            }
        )
        tool = self.add(FakeTool(spec))

        missing = self.manager.call("demo.echo", args={})
        extra = self.manager.call("demo.echo", args={"query": "abc", "x": 1})

        self.assertEqual(missing.error.code, ERROR_INVALID_INPUT)
        self.assertEqual(extra.error.code, ERROR_INVALID_INPUT)
        self.assertEqual(tool.calls, [])

    def test_tool_returning_wrong_type_is_internal_error(self) -> None:
        tool = self.add(FakeTool(result_factory=lambda n, kw: {"ok": True}))

        result = self.manager.call("demo.echo")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INTERNAL)
        self.assertEqual(len(tool.calls), 1)

    def test_tool_raising_is_internal_error(self) -> None:
        def boom(n, kw):
            raise RuntimeError("炸了")

        self.add(FakeTool(result_factory=boom))
        result = self.manager.call("demo.echo")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INTERNAL)
        self.assertIn("RuntimeError", result.error.message)

    def test_output_schema_is_soft_validation(self) -> None:
        spec = make_spec(
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
                "additionalProperties": False,
            }
        )
        tool = self.add(FakeTool(spec, result_factory=lambda n, kw: ToolResult.success({"foo": 1})))

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok, "输出不符合 schema 不应把可用结果判死")
        self.assertEqual(tool.calls, [{}])
        self.assertFalse(result.usage["output_valid"])
        self.assertTrue(result.usage["output_problems"])
        self.assertTrue(self.audit.read()[0]["output_invalid"])

    def test_passing_output_schema_stays_clean(self) -> None:
        spec = make_spec(
            output_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            }
        )
        self.add(FakeTool(spec, result_factory=lambda n, kw: ToolResult.success({"status": "ok"})))

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok)
        self.assertNotIn("output_valid", result.usage)
        self.assertFalse(self.audit.read()[0]["output_invalid"])

    def test_trace_id_reaches_audit(self) -> None:
        self.add(FakeTool())
        ctx = ToolContext(trace_id="trace-123")

        self.manager.call("demo.echo", ctx=ctx)

        self.assertEqual(self.audit.read()[0]["trace_id"], "trace-123")


class PermissionTests(ManagerFixture):
    def test_external_disabled_by_default(self) -> None:
        tool = self.add(FakeTool(make_spec(external=True)))

        result = self.manager.call("demo.echo")

        self.assertEqual(result.error.code, ERROR_PERMISSION)
        self.assertEqual(result.error.detail, "external_disabled")
        self.assertEqual(tool.calls, [])

    def test_external_allowed_by_policy(self) -> None:
        self.manager.policy = ToolPolicy(allowed_external=True)
        tool = self.add(FakeTool(make_spec(external=True)))

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok)
        self.assertEqual(len(tool.calls), 1)

    def test_tool_disabled_by_list(self) -> None:
        tool = self.add(FakeTool())
        self.manager.policy = ToolPolicy(enabled_tools=("other.tool",))

        result = self.manager.call("demo.echo")

        self.assertEqual(result.error.detail, "tool_disabled")
        self.assertEqual(tool.calls, [])

    def test_side_effect_disabled(self) -> None:
        tool = self.add(FakeTool(make_spec(side_effect="write_local")))

        result = self.manager.call("demo.echo")

        self.assertEqual(result.error.detail, "side_effect_disabled")
        self.assertEqual(tool.calls, [])

    def test_skill_cannot_use_unflagged_tool(self) -> None:
        tool = self.add(FakeTool(make_spec(allowed_for_skills=False)))

        result = self.manager.call("demo.echo", caller="skill")

        self.assertEqual(result.error.detail, "skill_not_allowed")
        self.assertEqual(tool.calls, [])

    def test_skill_must_declare_external_tool(self) -> None:
        self.manager.policy = ToolPolicy(allowed_external=True)
        tool = self.add(FakeTool(make_spec(external=True)))

        denied = self.manager.call("demo.echo", caller="skill")
        allowed_by_name = self.manager.call(
            "demo.echo", caller="skill", declared=("demo.echo",)
        )
        allowed_by_capability = self.manager.call(
            "demo.echo", caller="skill", declared=("echo",)
        )

        self.assertEqual(denied.error.detail, "undeclared")
        self.assertTrue(allowed_by_name.ok)
        self.assertTrue(allowed_by_capability.ok)
        self.assertEqual(len(tool.calls), 2)

    def test_script_caller_does_not_need_declaration(self) -> None:
        self.manager.policy = ToolPolicy(allowed_external=True)
        self.add(FakeTool(make_spec(external=True)))

        self.assertTrue(self.manager.call("demo.echo").ok)


class PathWhitelistTests(ManagerFixture):
    def read_tool(self) -> FakeTool:
        spec = make_spec(
            side_effect="read_local",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
        return self.add(FakeTool(spec))

    def test_temp_path_is_allowed(self) -> None:
        tool = self.read_tool()
        target = self.root / "a.txt"
        target.write_text("x", encoding="utf-8")

        result = self.manager.call("demo.echo", args={"path": str(target)})

        self.assertTrue(result.ok)
        self.assertEqual(len(tool.calls), 1)

    def test_path_outside_whitelist_is_rejected(self) -> None:
        tool = self.read_tool()

        result = self.manager.call("demo.echo", args={"path": str(Path(__file__).resolve())})

        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
        self.assertIn("path", result.error.message)
        self.assertEqual(tool.calls, [])

    def test_traversal_is_rejected(self) -> None:
        tool = self.read_tool()
        escaped = self.root / ".." / ".." / "outside.txt"

        result = self.manager.call("demo.echo", args={"path": str(escaped)})

        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
        self.assertEqual(tool.calls, [])

    def test_non_string_path_is_rejected(self) -> None:
        tool = self.read_tool()

        result = self.manager.call("demo.echo", args={"path": 123})

        self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
        self.assertIn("必须是字符串路径", result.error.message)
        self.assertEqual(tool.calls, [])

    def test_whitelist_can_be_disabled(self) -> None:
        self.manager.policy = ToolPolicy(enforce_path_whitelist=False)
        tool = self.read_tool()

        result = self.manager.call("demo.echo", args={"path": str(Path(__file__).resolve())})

        self.assertTrue(result.ok)
        self.assertEqual(len(tool.calls), 1)

    def test_check_path_helper(self) -> None:
        self.assertIsNone(check_path(str(self.root)))
        self.assertIsNotNone(check_path(str(Path(__file__).resolve())))
        self.assertIsNotNone(check_path(""))
        self.assertIsNotNone(check_path(123))


class RetryTests(ManagerFixture):
    def external_tool(self, factory, *, max_attempts=3, retry_on=()):
        spec = make_spec(
            external=True,
            timeout_s=30.0,
            retry=RetryPolicy(
                max_attempts=max_attempts,
                backoff_s=0.5,
                retry_on=retry_on,
            ),
        )
        self.manager.policy = ToolPolicy(allowed_external=True)
        return self.add(FakeTool(spec, result_factory=factory))

    def test_retryable_failure_is_retried(self) -> None:
        tool = self.external_tool(lambda n, kw: failing())

        result = self.manager.call("demo.echo")

        self.assertFalse(result.ok)
        self.assertEqual(len(tool.calls), 3)
        self.assertEqual(result.usage["attempts"], 3)
        self.assertEqual(self.sleeps, [0.5, 1.0])

    def test_success_after_retry(self) -> None:
        tool = self.external_tool(
            lambda n, kw: ToolResult.success({"ok": True}) if n == 2 else failing()
        )

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok)
        self.assertEqual(len(tool.calls), 2)
        self.assertEqual(result.usage["attempts"], 2)

    def test_non_retryable_failure_is_not_retried(self) -> None:
        tool = self.external_tool(lambda n, kw: failing(retryable=False))

        self.manager.call("demo.echo")

        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_retry_on_filter(self) -> None:
        tool = self.external_tool(
            lambda n, kw: failing(code="timeout"),
            retry_on=("upstream_error",),
        )

        self.manager.call("demo.echo")

        self.assertEqual(len(tool.calls), 1)

    def test_write_tool_is_never_retried(self) -> None:
        spec = make_spec(
            side_effect="write_local",
            retry=RetryPolicy(max_attempts=3, backoff_s=0.1),
        )
        self.manager.policy = ToolPolicy(
            allowed_side_effects=("none", "read_local", "write_local")
        )
        tool = self.add(FakeTool(spec, result_factory=lambda n, kw: failing()))

        self.manager.call("demo.echo")

        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(self.sleeps, [])


class TimeoutTests(ManagerFixture):
    def test_external_read_only_gets_hard_timeout(self) -> None:
        def slow(n, kw):
            time.sleep(0.3)
            return ToolResult.success({"ok": True})

        spec = make_spec(external=True, side_effect="none", timeout_s=0.05)
        self.manager.policy = ToolPolicy(allowed_external=True)
        self.add(FakeTool(spec, result_factory=slow))

        result = self.manager.call("demo.echo")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_TIMEOUT)
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.usage["attempts"], 1)
        self.assertEqual(result.usage["timeout_s"], 0.05)
        self.assertTrue(self.audit.read()[0]["over_timeout"])

    def test_local_tool_gets_elapsed_marker_not_thread_timeout(self) -> None:
        def slow(n, kw):
            time.sleep(0.12)
            return ToolResult.success({"ok": True})

        spec = make_spec(side_effect="read_local", external=False, timeout_s=0.05)
        self.add(FakeTool(spec, result_factory=slow))

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok, "本机工具不应被线程强等中断")
        self.assertTrue(result.usage["over_timeout"])
        self.assertTrue(self.audit.read()[0]["over_timeout"])


class CircuitBreakerTests(ManagerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.manager.policy = ToolPolicy(
            allowed_external=True, failure_threshold=2, cooldown_s=10.0
        )
        self.state = {"fail": True}

        def factory(n, kw):
            if self.state["fail"]:
                return failing()
            return ToolResult.success({"ok": True})

        self.tool = self.add(
            FakeTool(make_spec(external=True, retry=RetryPolicy(max_attempts=1)), factory)
        )

    def test_open_then_half_open_then_closed(self) -> None:
        self.manager.call("demo.echo")
        self.manager.call("demo.echo")
        self.assertEqual(self.manager.circuit_state("demo.echo"), "open")

        blocked = self.manager.call("demo.echo")
        self.assertEqual(blocked.error.code, ERROR_CIRCUIT_OPEN)
        self.assertEqual(len(self.tool.calls), 2)

        self.now[0] += 11
        self.assertEqual(self.manager.circuit_state("demo.echo"), "half_open")

        self.state["fail"] = False
        recovered = self.manager.call("demo.echo")

        self.assertTrue(recovered.ok)
        self.assertEqual(self.manager.circuit_state("demo.echo"), "closed")
        self.assertEqual(len(self.tool.calls), 3)

    def test_non_retryable_failures_do_not_open_breaker(self) -> None:
        local = FakeTool(
            make_spec(name="demo.local", retry=RetryPolicy(max_attempts=1)),
            result_factory=lambda n, kw: failing(code="invalid_input", retryable=False),
        )
        self.add(local)

        self.manager.call("demo.local")
        self.manager.call("demo.local")

        self.assertEqual(self.manager.circuit_state("demo.local"), "closed")

    def test_reset_closes(self) -> None:
        self.manager.call("demo.echo")
        self.manager.call("demo.echo")
        self.manager.reset()
        self.assertEqual(self.manager.circuit_state("demo.echo"), "closed")


class AuditTests(ManagerFixture):
    def test_audit_write_failure_does_not_break_call(self) -> None:
        blocked = self.root / "blocked"
        blocked.write_text("x", encoding="utf-8")
        self.manager.audit = AuditLogger(blocked / "sub")
        self.add(FakeTool())

        result = self.manager.call("demo.echo")

        self.assertTrue(result.ok)

    def test_read_skips_corrupt_lines(self) -> None:
        path = self.audit.path_for()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"tool":"a"}\nnot-json\n\n{"tool":"b"}\n', encoding="utf-8")

        records = self.audit.read()

        self.assertEqual([item["tool"] for item in records], ["a", "b"])


class PolicyValidationTests(unittest.TestCase):
    def test_invalid_policies_are_rejected(self) -> None:
        cases = (
            {"allowed_external": "yes"},
            {"enabled_tools": ["demo.echo"]},
            {"enabled_tools": ("",)},
            {"allowed_side_effects": ("anything",)},
            {"allowed_side_effects": ["none"]},
            {"enforce_path_whitelist": "yes"},
            {"failure_threshold": 0},
            {"failure_threshold": True},
            {"cooldown_s": -1},
            {"cooldown_s": "x"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ToolPolicy(**kwargs)


class ImportIsolationTests(unittest.TestCase):
    def test_import_app_plugins_does_not_load_adapters_or_clients(self) -> None:
        code = (
            "import sys; import app.plugins; "
            "print(int('app.plugins.adapters' in sys.modules), "
            "int('app.clients.lightrag' in sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "0 0")


if __name__ == "__main__":
    unittest.main()
