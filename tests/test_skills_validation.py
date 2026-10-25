"""P2D：Skill Validator 运行器与 Manager 接入（全离线）。"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from app.services.context_builder import Citation
from app.services.validators import Issue, ValidationReport
from app.skills import (
    SkillInput,
    SkillManager,
    SkillOutput,
    SkillRegistry,
    SkillSpec,
)
from app.skills.validation import (
    DEFAULT_VALIDATORS,
    available_validators,
    run_validators,
)


def citation(label: str, *, kind: str = "image", reference_id: str = "") -> Citation:
    return Citation(
        citation_id=f"c-{label}",
        label=label,
        kind=kind,
        reference_id=reference_id,
    )


class FakeValidator:
    name = "fake"
    version = "1"

    def __init__(self, report=None, *, error: Exception | None = None) -> None:
        self.report = report
        self.error = error
        self.payloads = []

    def validate(self, payload):
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.report


class RegistryTests(unittest.TestCase):
    def test_default_registry_contains_both_validators(self) -> None:
        self.assertEqual(
            available_validators(), ("citation", "citation_labels")
        )
        self.assertIn("citation", DEFAULT_VALIDATORS)
        self.assertIn("citation_labels", DEFAULT_VALIDATORS)


class RunnerTests(unittest.TestCase):
    def test_numeric_validator_summary(self) -> None:
        summary = run_validators(
            ("citation",),
            answer="答案 [1]",
            citations=[citation("[1]", kind="reference", reference_id="1")],
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["error_count"], 0)
        self.assertEqual(summary["warning_count"], 0)
        self.assertEqual(summary["cited_count"], 1)
        self.assertEqual(summary["unknown_ids"], [])
        self.assertEqual(summary["validators"][0]["name"], "citation")

    def test_label_validator_summary(self) -> None:
        summary = run_validators(
            ("citation_labels",),
            answer="[img-1] 书桌照片",
            citations=[citation("[img-1]")],
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["cited_count"], 1)
        self.assertEqual(summary["validators"][0]["name"], "citation_labels")

    def test_validation_errors_are_summarized_not_raised(self) -> None:
        summary = run_validators(
            ("citation",),
            answer="答案 [9]",
            citations=[citation("[1]", kind="reference", reference_id="1")],
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["unknown_ids"], ["9"])
        self.assertIn("unknown_citation", summary["issue_codes"])

    def test_warnings_do_not_fail_the_summary(self) -> None:
        summary = run_validators(
            ("citation_labels",),
            answer="没有标记",
            citations=[citation("[img-1]")],
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["warning_count"], 1)
        self.assertEqual(summary["issue_codes"], ["missing_citation"])

    def test_unknown_validator_is_reported(self) -> None:
        summary = run_validators(
            ("does_not_exist",),
            answer="答案",
            citations=[],
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["issue_codes"], ["unknown_validator"])
        self.assertEqual(summary["validators"][0]["error"], "unknown_validator")

    def test_validator_exception_is_contained(self) -> None:
        secret = "异常里的机密正文"
        summary = run_validators(
            ("boom",),
            answer=secret,
            citations=[],
            registry={"boom": FakeValidator(error=RuntimeError(secret))},
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["issue_codes"], ["validator_exception"])
        self.assertNotIn(secret, json.dumps(summary, ensure_ascii=False))

    def test_invalid_report_is_contained(self) -> None:
        summary = run_validators(
            ("bad",),
            answer="答案",
            citations=[],
            registry={"bad": FakeValidator(report={"ok": True})},
        )

        self.assertFalse(summary["ok"])
        self.assertEqual(summary["issue_codes"], ["invalid_report"])

    def test_duplicate_declarations_run_once(self) -> None:
        fake = FakeValidator(
            report=ValidationReport("fake", "1")
        )
        summary = run_validators(
            ("fake", "fake"),
            answer="答案",
            citations=[],
            registry={"fake": fake},
        )

        self.assertEqual(len(fake.payloads), 1)
        self.assertEqual(len(summary["validators"]), 1)

    def test_single_string_name_is_accepted(self) -> None:
        summary = run_validators(
            "citation_labels",
            answer="[img-1]",
            citations=[citation("[img-1]")],
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["validators"][0]["name"], "citation_labels")

    def test_empty_names_return_empty_summary(self) -> None:
        summary = run_validators((), answer="答案", citations=[])

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["validators"], [])
        self.assertEqual(summary["error_count"], 0)

    def test_summary_drops_issue_messages_and_arbitrary_stats(self) -> None:
        secret = "机密答案正文"
        report = ValidationReport("fake", "1")
        report.add(Issue("unknown_citation", f"消息里带了{secret}", "error", "img-9"))
        report.stats.update(
            {
                "cited_count": 2,
                "unknown_ids": ["img-9"],
                "answer_text": secret,
            }
        )

        summary = run_validators(
            ("fake",),
            answer=secret,
            citations=[],
            registry={"fake": FakeValidator(report=report)},
        )
        rendered = json.dumps(summary, ensure_ascii=False)

        self.assertEqual(summary["unknown_ids"], ["img-9"])
        self.assertEqual(summary["cited_count"], 2)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("消息里带了", rendered)
        self.assertNotIn("answer_text", summary)


class ManagerIntegrationTests(unittest.TestCase):
    class Skill:
        def __init__(self, spec: SkillSpec, output: SkillOutput) -> None:
            self.spec = spec
            self.output = output

        def can_handle(self, request: SkillInput) -> float:
            return 1.0

        def execute(self, request: SkillInput, ctx) -> SkillOutput:
            return self.output

    def make_manager(self, *, validators=("citation",), output=None):
        spec = SkillSpec(
            name="demo_skill",
            version="0.1.0",
            description="示例技能",
            validators=validators,
        )
        output = output or SkillOutput(
            answer="答案 [9]",
            citations=[citation("[1]", kind="reference", reference_id="1")],
        )
        registry = SkillRegistry()
        registry.register(self.Skill(spec, output))
        return SkillManager(registry)

    def test_validation_is_recorded_but_does_not_change_status(self) -> None:
        manager = self.make_manager()

        result = manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        validation = result.trace["detail"]["validation"]
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["error_count"], 1)
        self.assertEqual(validation["unknown_ids"], ["9"])

    def test_no_declared_validators_leaves_trace_unchanged(self) -> None:
        manager = self.make_manager(validators=())

        result = manager.run(SkillInput("问题"))

        self.assertNotIn("validation", result.trace["detail"])

    def test_validator_exception_is_contained(self) -> None:
        manager = self.make_manager()

        with mock.patch(
            "app.skills.manager.run_validators",
            side_effect=RuntimeError("校验器炸了"),
        ):
            result = manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        validation = result.trace["detail"]["validation"]
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["issue_codes"], ["validator_exception"])

    def test_validator_time_is_not_counted_as_skill_timeout(self) -> None:
        now = [100.0]
        spec = SkillSpec(
            name="demo_skill",
            version="0.1.0",
            description="示例技能",
            validators=("citation",),
            timeout_s=1.0,
        )
        registry = SkillRegistry()
        registry.register(
            self.Skill(
                spec,
                SkillOutput(
                    answer="答案 [1]",
                    citations=[citation("[1]", kind="reference", reference_id="1")],
                ),
            )
        )
        manager = SkillManager(registry, clock=lambda: now[0])

        def slow_validators(*args, **kwargs):
            now[0] += 2.0
            return {
                "ok": True,
                "validators": [],
                "issue_codes": [],
                "error_count": 0,
                "warning_count": 0,
                "cited_count": 1,
                "unknown_ids": [],
            }

        with mock.patch("app.skills.manager.run_validators", side_effect=slow_validators):
            result = manager.run(SkillInput("问题"))

        self.assertEqual(result.status, "ok")
        self.assertFalse(result.trace["over_timeout"])


if __name__ == "__main__":
    unittest.main()
