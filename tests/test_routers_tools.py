"""P1A：GET /api/tools 的离线用例。

约定：注册表与策略由测试注入；不发请求、不探活、不碰真实 data/ 与 logs/。
"""

from __future__ import annotations

import json
import unittest

from app.plugins import (
    Health,
    RetryPolicy,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    load_builtin_tools,
)
from app.routers import all_routers
from app.routers import tools as tools_router


def spec(name: str, **overrides) -> ToolSpec:
    values = {
        "name": name,
        "version": "0.2.0",
        "description": "示例",
        "input_schema": {"type": "object"},
        "output_schema": {},
        "capabilities": ("demo",),
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
    def __init__(self, tool_spec: ToolSpec, *, health_raises: bool = False) -> None:
        self.spec = tool_spec
        self.health_raises = health_raises

    def invoke(self, ctx=None, **kwargs):
        return ToolResult.success({})

    def health(self, ctx=None):
        if self.health_raises:
            raise AssertionError("清单接口不应触发健康探测")
        return Health(ok=True)


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        tools_router.reset_registry_cache()
        self.registry = ToolRegistry()
        self.registry.register(FakeTool(spec("demo.b")))
        self.registry.register(
            FakeTool(spec("demo.a", external=True, capabilities=("retrieval",)))
        )
        self.registry.register(
            FakeTool(spec("demo.c", side_effect="read_local", allowed_for_skills=False))
        )

    def test_rows_are_sorted_and_complete(self) -> None:
        payload = tools_router.tool_catalog(registry=self.registry)

        self.assertTrue(payload["ok"])
        self.assertEqual([row["name"] for row in payload["tools"]], ["demo.a", "demo.b", "demo.c"])
        row = payload["tools"][1]
        self.assertEqual(
            set(row),
            {
                "name",
                "version",
                "description",
                "capabilities",
                "side_effect",
                "external",
                "risk",
                "timeout_s",
                "retry",
                "allowed_for_skills",
                "enabled",
                "policy_reason",
            },
        )
        self.assertEqual(
            row["retry"], {"max_attempts": 1, "backoff_s": 0.0, "retry_on": []}
        )

    def test_default_policy_disables_external_tools(self) -> None:
        payload = tools_router.tool_catalog(registry=self.registry)
        rows = {row["name"]: row for row in payload["tools"]}

        self.assertFalse(rows["demo.a"]["enabled"])
        self.assertEqual(rows["demo.a"]["policy_reason"], "external_disabled")
        self.assertTrue(rows["demo.b"]["enabled"])
        self.assertEqual(rows["demo.b"]["policy_reason"], "ok")
        self.assertTrue(rows["demo.c"]["enabled"])
        self.assertFalse(payload["policy"]["allowed_external"])

    def test_policy_can_enable_external_tools(self) -> None:
        payload = tools_router.tool_catalog(
            registry=self.registry,
            policy=ToolPolicy(allowed_external=True),
        )
        rows = {row["name"]: row for row in payload["tools"]}

        self.assertTrue(rows["demo.a"]["enabled"])
        self.assertTrue(payload["policy"]["allowed_external"])

    def test_schema_is_opt_in(self) -> None:
        plain = tools_router.tool_catalog(registry=self.registry)
        detailed = tools_router.tool_catalog(registry=self.registry, with_schema=True)

        self.assertNotIn("input_schema", plain["tools"][0])
        self.assertEqual(detailed["tools"][0]["input_schema"], {"type": "object"})

    def test_payload_is_json_serializable(self) -> None:
        payload = tools_router.tool_catalog(registry=self.registry, with_schema=True)
        json.dumps(payload, ensure_ascii=False)

    def test_health_is_never_probed(self) -> None:
        registry = ToolRegistry()
        registry.register(FakeTool(spec("demo.a"), health_raises=True))

        payload = tools_router.tool_catalog(registry=registry)

        self.assertEqual(len(payload["tools"]), 1)


class BuiltinCatalogTests(unittest.TestCase):
    def test_builtin_tools_are_listed(self) -> None:
        payload = tools_router.tool_catalog(registry=load_builtin_tools())
        rows = {row["name"]: row for row in payload["tools"]}

        self.assertEqual(
            sorted(rows),
            ["knowledge.search", "ocr.recognize", "parse.document"],
        )
        self.assertFalse(rows["knowledge.search"]["enabled"])
        self.assertEqual(rows["knowledge.search"]["policy_reason"], "external_disabled")
        self.assertTrue(rows["parse.document"]["enabled"])
        self.assertFalse(rows["ocr.recognize"]["enabled"])
        # P1A：检索允许重试一次（只读），且 input_schema 拒绝未知参数
        self.assertEqual(rows["knowledge.search"]["retry"]["max_attempts"], 2)
        self.assertEqual(
            rows["knowledge.search"]["retry"]["retry_on"], ["upstream_error"]
        )

    def test_default_registry_is_cached(self) -> None:
        first = tools_router.default_registry()
        second = tools_router.default_registry()

        self.assertIs(first, second)
        self.assertEqual(
            first.names(), ["knowledge.search", "ocr.recognize", "parse.document"]
        )

    def test_router_function_and_route(self) -> None:
        payload = tools_router.list_tools(False)

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["tools"]), 3)
        paths = {route.path for route in tools_router.router.routes}
        self.assertIn("/api/tools", paths)

    def test_tools_router_is_registered(self) -> None:
        self.assertIn(tools_router.router, all_routers)

    def tearDown(self) -> None:
        tools_router.reset_registry_cache()


if __name__ == "__main__":
    unittest.main()
