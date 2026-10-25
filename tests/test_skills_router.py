"""P2A：规则路由的离线单测（无网络、无磁盘）。"""

from __future__ import annotations

import unittest

from app.skills import (
    SkillInput,
    SkillOutput,
    SkillRegistry,
    SkillRouter,
    SkillSpec,
)


def make_spec(name: str, *, priority: int = 0, enabled: bool = True) -> SkillSpec:
    return SkillSpec(
        name=name,
        version="0.1.0",
        description="示例技能",
        priority=priority,
        enabled_by_default=enabled,
    )


class FakeSkill:
    def __init__(
        self,
        name: str,
        *,
        confidence=1.0,
        priority: int = 0,
        enabled: bool = True,
        handler=None,
    ) -> None:
        self.spec = make_spec(name, priority=priority, enabled=enabled)
        self._confidence = confidence
        self._handler = handler

    def can_handle(self, request: SkillInput) -> float:
        if self._handler is not None:
            return self._handler(request)
        return self._confidence

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        return SkillOutput(answer=self.spec.name)


class RouterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = SkillRegistry()
        self.request = SkillInput("问题")

    def add(self, skill: FakeSkill) -> FakeSkill:
        self.registry.register(skill)
        return skill

    def router(self, **kwargs) -> SkillRouter:
        return SkillRouter(self.registry, **kwargs)


class SelectionTests(RouterFixture):
    def test_selects_highest_confidence(self) -> None:
        self.add(FakeSkill("low_skill", confidence=0.6))
        self.add(FakeSkill("high_skill", confidence=0.9))

        decision = self.router().select(self.request)

        self.assertEqual(decision.name, "high_skill")
        self.assertAlmostEqual(decision.confidence, 0.9)
        self.assertEqual(decision.reason, "rule")

    def test_threshold_is_inclusive(self) -> None:
        self.add(FakeSkill("below_skill", confidence=0.49))
        self.add(FakeSkill("exact_skill", confidence=0.5))

        decision = self.router(min_confidence=0.5).select(self.request)

        self.assertEqual(decision.name, "exact_skill")

    def test_tie_breaks_by_priority_then_name(self) -> None:
        self.add(FakeSkill("alpha_skill", confidence=0.8, priority=0))
        self.add(FakeSkill("beta_skill", confidence=0.8, priority=2))
        self.add(FakeSkill("gamma_skill", confidence=0.8, priority=2))

        decision = self.router().select(self.request)

        self.assertEqual(decision.name, "beta_skill")

    def test_disabled_skill_is_skipped(self) -> None:
        self.add(FakeSkill("disabled_skill", enabled=False))

        decision = self.router().select(self.request)

        self.assertIsNone(decision.skill)
        self.assertEqual(decision.name, "")
        self.assertEqual(decision.reason, "none")

    def test_no_candidate_returns_none(self) -> None:
        self.add(FakeSkill("low_skill", confidence=0.1))

        decision = self.router().select(self.request)

        self.assertIsNone(decision.skill)
        self.assertEqual(decision.reason, "none")


class FallbackTests(RouterFixture):
    def test_fallback_is_used_when_no_match(self) -> None:
        self.add(FakeSkill("low_skill", confidence=0.1))
        fallback = self.add(FakeSkill("fallback_skill", confidence=0.1))

        decision = self.router(fallback="fallback_skill").select(self.request)

        self.assertIs(decision.skill, fallback)
        self.assertEqual(decision.name, "fallback_skill")
        self.assertEqual(decision.reason, "fallback")

    def test_fallback_can_be_disabled(self) -> None:
        fallback = self.add(FakeSkill("fallback_skill", enabled=False))

        decision = self.router(fallback="fallback_skill").select(self.request)

        self.assertIs(decision.skill, fallback)

    def test_missing_fallback_is_reported(self) -> None:
        decision = self.router(fallback="missing_skill").select(self.request)

        self.assertIsNone(decision.skill)
        self.assertEqual(decision.reason, "fallback_missing")

    def test_fallback_is_not_used_when_primary_matches(self) -> None:
        primary = self.add(FakeSkill("primary_skill", confidence=0.9))
        self.add(FakeSkill("fallback_skill", confidence=0.1))

        decision = self.router(fallback="fallback_skill").select(self.request)

        self.assertIs(decision.skill, primary)
        self.assertEqual(decision.reason, "rule")


class ForceTests(RouterFixture):
    def test_force_overrides_threshold_and_enabled(self) -> None:
        forced = self.add(FakeSkill("forced_skill", confidence=0.0, enabled=False))

        decision = self.router(fallback="other_skill").select(
            self.request, force="forced_skill"
        )

        self.assertIs(decision.skill, forced)
        self.assertEqual(decision.reason, "force")
        self.assertEqual(decision.confidence, 1.0)

    def test_missing_force_is_reported(self) -> None:
        decision = self.router().select(self.request, force="missing_skill")

        self.assertIsNone(decision.skill)
        self.assertEqual(decision.reason, "force_missing")


class ScoreTests(RouterFixture):
    def test_bad_values_score_zero(self) -> None:
        values = [None, "0.9", float("nan"), float("inf"), object()]

        for value in values:
            with self.subTest(value=value):
                skill = FakeSkill("bad_skill", handler=lambda request, value=value: value)
                self.assertEqual(self.router().score(skill, self.request), 0.0)

    def test_exception_scores_zero(self) -> None:
        def explode(request):
            raise RuntimeError("规则炸了")

        skill = FakeSkill("bad_skill", handler=explode)

        self.assertEqual(self.router().score(skill, self.request), 0.0)

    def test_bool_is_supported(self) -> None:
        yes = FakeSkill("yes_skill", handler=lambda request: True)
        no = FakeSkill("no_skill", handler=lambda request: False)

        self.assertEqual(self.router().score(yes, self.request), 1.0)
        self.assertEqual(self.router().score(no, self.request), 0.0)

    def test_scores_are_clamped(self) -> None:
        high = FakeSkill("high_skill", handler=lambda request: 1.5)
        low = FakeSkill("low_skill", handler=lambda request: -0.5)

        self.assertEqual(self.router().score(high, self.request), 1.0)
        self.assertEqual(self.router().score(low, self.request), 0.0)


class ConstructorTests(RouterFixture):
    def test_invalid_min_confidence_is_rejected(self) -> None:
        for value in (-0.1, 1.1, True, "0.5", float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SkillRouter(self.registry, min_confidence=value)

    def test_invalid_fallback_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SkillRouter(self.registry, fallback=1)  # type: ignore[arg-type]

    def test_invalid_registry_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            SkillRouter("not-a-registry")  # type: ignore[arg-type]

    def test_decision_to_dict(self) -> None:
        skill = self.add(FakeSkill("demo_skill", confidence=0.8))

        decision = self.router().select(self.request)

        self.assertEqual(
            decision.to_dict(),
            {"skill": "demo_skill", "confidence": 0.8, "reason": "rule"},
        )
        self.assertIs(decision.skill, skill)


if __name__ == "__main__":
    unittest.main()
