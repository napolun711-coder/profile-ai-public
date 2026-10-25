"""P0：Tool 契约与注册表单测（全离线，无网络、无临时文件）。"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from app.plugins import (
    Evidence,
    Health,
    RetryPolicy,
    ToolContext,
    ToolError,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    DuplicateToolError,
    load_builtin_tools,
    validate_spec,
)

ROOT = Path(__file__).resolve().parents[1]


def make_spec(**overrides):
    values = {
        "name": "demo.echo",
        "version": "0.1.0",
        "description": "示例工具",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
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


class FakeTool:
    def __init__(self, name: str = "demo.echo") -> None:
        self.spec = make_spec(name=name)

    def invoke(self, ctx=None, **kwargs):
        return ToolResult.success(kwargs)

    def health(self, ctx=None):
        return Health(ok=True)


class ToolSpecTests(unittest.TestCase):
    def test_valid_spec_passes(self) -> None:
        validate_spec(make_spec())

    def test_invalid_specs_are_rejected(self) -> None:
        cases = {
            "name_without_dot": {"name": "echo"},
            "name_with_uppercase": {"name": "Demo.echo"},
            "empty_version": {"version": ""},
            "empty_description": {"description": "  "},
            "input_schema_not_dict": {"input_schema": []},
            "output_schema_not_dict": {"output_schema": []},
            "capabilities_not_tuple": {"capabilities": ["echo"]},
            "unknown_side_effect": {"side_effect": "read"},
            "external_not_bool": {"external": "yes"},
            "unknown_risk": {"risk": "critical"},
            "zero_timeout": {"timeout_s": 0},
            "string_timeout": {"timeout_s": "5"},
            "bool_timeout": {"timeout_s": True},
            "bad_retry": {"retry": "retry"},
            "skills_not_bool": {"allowed_for_skills": "yes"},
            "config_schema_not_dict": {"config_schema": []},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    validate_spec(make_spec(**overrides))


class RetryPolicyTests(unittest.TestCase):
    def test_defaults(self) -> None:
        policy = RetryPolicy()
        self.assertEqual(policy.max_attempts, 1)
        self.assertEqual(policy.backoff_s, 0.0)
        self.assertEqual(policy.retry_on, ())

    def test_invalid_values_are_rejected(self) -> None:
        for kwargs in (
            {"max_attempts": 0},
            {"max_attempts": "1"},
            {"backoff_s": -0.1},
            {"backoff_s": "x"},
            {"retry_on": ["timeout"]},
            {"retry_on": ("",)},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RetryPolicy(**kwargs)


class ResultTests(unittest.TestCase):
    def test_success_cannot_carry_error(self) -> None:
        with self.assertRaises(ValueError):
            ToolResult(ok=True, error=ToolError(code="x", message="boom"))

    def test_failure_must_carry_error(self) -> None:
        with self.assertRaises(ValueError):
            ToolResult(ok=False)

    def test_helper_constructors(self) -> None:
        ok = ToolResult.success({"a": 1}, usage={"elapsed_ms": 1.2})
        self.assertTrue(ok.ok)
        self.assertIsNone(ok.error)
        self.assertEqual(ok.usage["elapsed_ms"], 1.2)

        failed = ToolResult.failure(ToolError(code="x", message="boom"), data={"raw": True})
        self.assertFalse(failed.ok)
        self.assertEqual(failed.data, {"raw": True})
        self.assertEqual(failed.error.code, "x")

    def test_tool_error_requires_code_and_message(self) -> None:
        with self.assertRaises(ValueError):
            ToolError(code="", message="boom")
        with self.assertRaises(ValueError):
            ToolError(code="x", message="")

    def test_tool_error_to_dict(self) -> None:
        error = ToolError(
            code="upstream_error",
            message="知识库不可用",
            retryable=True,
            detail="boom",
            upstream_status=503,
        )
        self.assertEqual(
            error.to_dict(),
            {
                "code": "upstream_error",
                "message": "知识库不可用",
                "retryable": True,
                "detail": "boom",
                "upstream_status": 503,
            },
        )
        self.assertNotIn(
            "upstream_status",
            ToolError(code="x", message="y").to_dict(),
        )

    def test_evidence_requires_id_and_kind(self) -> None:
        with self.assertRaises(ValueError):
            Evidence(evidence_id="", kind="chunk")
        with self.assertRaises(ValueError):
            Evidence(evidence_id="chunk:1", kind="")

    def test_evidence_metadata_is_not_shared(self) -> None:
        first = Evidence(evidence_id="a", kind="chunk")
        second = Evidence(evidence_id="b", kind="chunk")
        first.metadata["x"] = 1
        self.assertEqual(second.metadata, {})

    def test_context_defaults(self) -> None:
        ctx = ToolContext()
        self.assertEqual(ctx.logger.name, "app.plugins")
        self.assertEqual(ctx.trace_id, "")
        self.assertIsNone(ctx.settings)


class RegistryTests(unittest.TestCase):
    def test_register_get_and_sorted_listing(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool("demo.b"))
        registry.register(FakeTool("demo.a"))

        self.assertEqual(registry.names(), ["demo.a", "demo.b"])
        self.assertEqual([tool.spec.name for tool in registry.all()], ["demo.a", "demo.b"])
        self.assertEqual(len(registry), 2)
        self.assertIn("demo.a", registry)
        self.assertNotIn("demo.c", registry)
        self.assertEqual(registry.get("demo.a").spec.name, "demo.a")

    def test_duplicate_registration_is_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool())

        with self.assertRaises(DuplicateToolError):
            registry.register(FakeTool())

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(ToolNotFoundError):
            ToolRegistry().get("missing.tool")
        with self.assertRaises(ToolNotFoundError):
            ToolRegistry().get(None)  # type: ignore[arg-type]

    def test_register_rejects_objects_without_contract(self) -> None:
        registry = ToolRegistry()
        with self.assertRaises(TypeError):
            registry.register(object())  # type: ignore[arg-type]

        class NoInvoke:
            spec = make_spec()

        with self.assertRaises(TypeError):
            registry.register(NoInvoke())  # type: ignore[arg-type]

        class NoHealth:
            spec = make_spec()

            def invoke(self, ctx=None, **kwargs):
                return ToolResult.success(kwargs)

        with self.assertRaises(TypeError):
            registry.register(NoHealth())  # type: ignore[arg-type]

    def test_clear(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool())
        registry.clear()
        self.assertEqual(registry.names(), [])


class BuiltinTests(unittest.TestCase):
    def test_builtin_names(self) -> None:
        registry = load_builtin_tools()
        self.assertEqual(
            registry.names(),
            ["knowledge.search", "ocr.recognize", "parse.document"],
        )
        for tool in registry.all():
            validate_spec(tool.spec)

    def test_builtin_loading_twice_is_rejected(self) -> None:
        registry = load_builtin_tools()
        with self.assertRaises(DuplicateToolError):
            load_builtin_tools(registry)

    def test_registry_module_import_does_not_load_adapters_or_clients(self) -> None:
        code = (
            "import sys; import app.plugins.registry; "
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
