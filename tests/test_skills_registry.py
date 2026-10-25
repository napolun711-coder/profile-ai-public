"""P2A：SkillRegistry 的离线单测（无网络、无磁盘）。"""

from __future__ import annotations

import unittest

from app.skills import (
    DuplicateSkillError,
    Skill,
    SkillInput,
    SkillNotFoundError,
    SkillOutput,
    SkillRegistry,
    SkillSpec,
)


def make_spec(name: str = "demo_skill", **overrides) -> SkillSpec:
    values = {
        "name": name,
        "version": "0.1.0",
        "description": "示例技能",
        "required_tools": ("demo.echo",),
    }
    values.update(overrides)
    return SkillSpec(**values)


class FakeSkill:
    def __init__(self, name: str = "demo_skill", **overrides) -> None:
        self.spec = make_spec(name, **overrides)
        self.calls = 0

    def can_handle(self, request: SkillInput) -> float:
        return 1.0

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        self.calls += 1
        return SkillOutput(answer="ok")


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry()

    def test_register_get_and_sort(self) -> None:
        beta = FakeSkill("beta_skill")
        alpha = FakeSkill("alpha_skill")

        self.assertIs(self.registry.register(beta), beta)
        self.registry.register(alpha)

        self.assertIs(self.registry.get("beta_skill"), beta)
        self.assertEqual(self.registry.names(), ["alpha_skill", "beta_skill"])
        self.assertEqual(len(self.registry), 2)
        self.assertIn("alpha_skill", self.registry)
        self.assertNotIn("missing", self.registry)

    def test_duplicate_is_rejected(self) -> None:
        self.registry.register(FakeSkill("demo_skill"))

        with self.assertRaises(DuplicateSkillError):
            self.registry.register(FakeSkill("demo_skill"))

        self.assertEqual(len(self.registry), 1)

    def test_missing_methods_are_rejected(self) -> None:
        class NoCanHandle:
            spec = make_spec("no_can_handle")

            def execute(self, request, ctx):
                return SkillOutput()

        class NoExecute:
            spec = make_spec("no_execute")

            def can_handle(self, request):
                return 1.0

        with self.assertRaises(TypeError):
            self.registry.register(NoCanHandle())
        with self.assertRaises(TypeError):
            self.registry.register(NoExecute())

    def test_missing_spec_is_rejected(self) -> None:
        class NoSpec:
            def can_handle(self, request):
                return 1.0

            def execute(self, request, ctx):
                return SkillOutput()

        with self.assertRaises(TypeError):
            self.registry.register(NoSpec())

    def test_invalid_spec_is_rejected(self) -> None:
        bad = FakeSkill("demo_skill", required_tools=("echo",))

        with self.assertRaises(ValueError):
            self.registry.register(bad)

        self.assertEqual(len(self.registry), 0)

    def test_unknown_name_raises(self) -> None:
        with self.assertRaises(SkillNotFoundError):
            self.registry.get("missing")
        with self.assertRaises(SkillNotFoundError):
            self.registry.get(1)  # type: ignore[arg-type]

    def test_clear(self) -> None:
        self.registry.register(FakeSkill("demo_skill"))
        self.registry.clear()

        self.assertEqual(len(self.registry), 0)
        self.assertEqual(self.registry.all(), [])

    def test_registered_skill_matches_protocol(self) -> None:
        skill = self.registry.register(FakeSkill("demo_skill"))

        self.assertIsInstance(skill, Skill)


if __name__ == "__main__":
    unittest.main()
