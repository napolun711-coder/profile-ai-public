"""P2A：Skill 契约与静态校验（全离线，无网络、无临时文件）。"""

from __future__ import annotations

import json
import unittest

from app.plugins import Evidence
from app.services.context_builder import Citation
from app.skills import (
    SKILL_STATUSES,
    Skill,
    SkillError,
    SkillInput,
    SkillOutput,
    SkillSpec,
    validate_skill_output,
    validate_skill_spec,
)


def make_spec(**overrides) -> SkillSpec:
    values = {
        "name": "demo_skill",
        "version": "0.1.0",
        "description": "示例技能",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "required_tools": ("demo.echo",),
        "validators": ("citation",),
        "max_steps": 4,
        "timeout_s": 180.0,
        "enabled_by_default": True,
        "keywords": ("示例",),
        "priority": 0,
    }
    values.update(overrides)
    return SkillSpec(**values)


def make_output(**overrides) -> SkillOutput:
    values = {
        "status": "ok",
        "answer": "答案 [1]",
        "evidence": [Evidence(evidence_id="chunk:1", kind="chunk", text="证据正文")],
        "citations": [
            Citation(citation_id="c1", label="[1]", kind="chunk", reference_id="1")
        ],
        "usage": {"tokens": 3},
    }
    values.update(overrides)
    return SkillOutput(**values)


class FakeSkill:
    spec = make_spec()

    def can_handle(self, request: SkillInput) -> float:
        return 1.0

    def execute(self, request: SkillInput, ctx) -> SkillOutput:
        return make_output()


class SpecTests(unittest.TestCase):
    def test_valid_spec_passes(self) -> None:
        validate_skill_spec(make_spec())

    def test_empty_required_tools_is_allowed(self) -> None:
        validate_skill_spec(make_spec(required_tools=()))

    def test_invalid_specs_are_rejected(self) -> None:
        cases = {
            "name_with_dot": {"name": "knowledge.qa"},
            "name_with_uppercase": {"name": "Knowledge_qa"},
            "name_starting_with_digit": {"name": "1skill"},
            "empty_version": {"version": ""},
            "empty_description": {"description": "  "},
            "input_schema_not_dict": {"input_schema": []},
            "output_schema_not_dict": {"output_schema": []},
            "required_tools_not_tuple": {"required_tools": ["demo.echo"]},
            "required_tool_without_dot": {"required_tools": ("echo",)},
            "required_tool_not_string": {"required_tools": (1,)},
            "validators_not_tuple": {"validators": ["citation"]},
            "zero_max_steps": {"max_steps": 0},
            "bool_max_steps": {"max_steps": True},
            "string_max_steps": {"max_steps": "4"},
            "zero_timeout": {"timeout_s": 0},
            "infinite_timeout": {"timeout_s": float("inf")},
            "string_timeout": {"timeout_s": "180"},
            "bool_timeout": {"timeout_s": True},
            "enabled_not_bool": {"enabled_by_default": "yes"},
            "keywords_not_tuple": {"keywords": ["示例"]},
            "priority_not_int": {"priority": "high"},
            "bool_priority": {"priority": True},
        }

        for label, overrides in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    validate_skill_spec(make_spec(**overrides))

    def test_non_spec_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_skill_spec("not-a-spec")  # type: ignore[arg-type]


class InputTests(unittest.TestCase):
    def test_defaults(self) -> None:
        request = SkillInput("问题")

        self.assertEqual(request.options, {})
        self.assertEqual(request.trace_id, "")

    def test_bad_types_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SkillInput(1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SkillInput("问题", options=[])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SkillInput("问题", trace_id=1)  # type: ignore[arg-type]


class OutputTests(unittest.TestCase):
    def test_to_dict_hides_answer_body_by_default(self) -> None:
        secret = "这是答案正文"
        output = make_output(
            answer=f"{secret} [1]",
            evidence=[Evidence(evidence_id="c1", kind="chunk", text="证据正文")],
            citations=[
                Citation(
                    citation_id="c1",
                    label="[1]",
                    kind="image",
                    caption="图注正文",
                )
            ],
        )

        rendered = json.dumps(output.to_dict(), ensure_ascii=False)

        self.assertNotIn(secret, rendered)
        self.assertNotIn("证据正文", rendered)
        self.assertNotIn("图注正文", rendered)
        self.assertNotIn("answer", output.to_dict())
        self.assertEqual(output.to_dict()["evidence_count"], 1)
        self.assertEqual(output.to_dict()["citation_count"], 1)

    def test_to_dict_can_include_answer_on_demand(self) -> None:
        output = make_output()

        payload = output.to_dict(include_answer=True)

        self.assertEqual(payload["answer"], "答案 [1]")
        self.assertEqual(payload["evidence"][0]["text"], "证据正文")
        self.assertEqual(payload["citations"][0]["label"], "[1]")

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SkillOutput(status="done")

    def test_bad_field_types_are_rejected(self) -> None:
        cases = [
            {"answer": 1},
            {"evidence": None},
            {"citations": None},
            {"usage": []},
            {"trace": []},
            {"error": "boom"},
        ]

        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    SkillOutput(**overrides)  # type: ignore[arg-type]

    def test_statuses_are_stable(self) -> None:
        self.assertEqual(SKILL_STATUSES, ("ok", "partial", "refused", "error"))


class ValidationTests(unittest.TestCase):
    def test_error_status_requires_error(self) -> None:
        output = SkillOutput(status="error")

        with self.assertRaises(ValueError):
            validate_skill_output(output)

    def test_non_error_status_cannot_carry_error(self) -> None:
        output = SkillOutput(status="ok", error=SkillError("boom", "失败"))

        with self.assertRaises(ValueError):
            validate_skill_output(output)

    def test_valid_error_output_passes(self) -> None:
        output = SkillOutput(
            status="error", error=SkillError("boom", "失败", retryable=True)
        )

        validate_skill_output(output)
        self.assertEqual(
            output.to_dict()["error"],
            {"code": "boom", "message": "失败", "retryable": True},
        )

    def test_non_output_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_skill_output({"status": "ok"})  # type: ignore[arg-type]


class ErrorTests(unittest.TestCase):
    def test_error_requires_code_and_message(self) -> None:
        with self.assertRaises(ValueError):
            SkillError("", "消息")
        with self.assertRaises(ValueError):
            SkillError("code", "  ")
        with self.assertRaises(ValueError):
            SkillError("code", "消息", retryable="yes")  # type: ignore[arg-type]

    def test_error_to_dict(self) -> None:
        self.assertEqual(
            SkillError("budget_exceeded", "超出预算").to_dict(),
            {
                "code": "budget_exceeded",
                "message": "超出预算",
                "retryable": False,
            },
        )


class ProtocolTests(unittest.TestCase):
    def test_fake_skill_matches_protocol(self) -> None:
        self.assertIsInstance(FakeSkill(), Skill)


if __name__ == "__main__":
    unittest.main()
