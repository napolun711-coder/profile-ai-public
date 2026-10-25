"""P2F：生产技能运行时（懒加载、工具策略、引用映射、回退判定）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.plugins import Evidence
from app.plugins.audit import AuditLogger
from app.services.context_builder import Citation
from app.skills import SkillOutput
from app.skills import production
from app.skills.builtin import image_lookup, knowledge_qa


def citation(
    *,
    reference_id: str = "1",
    doc_name: str = "文档.md",
    kind: str = "reference",
    **extra,
) -> Citation:
    return Citation(
        citation_id=f"citation:{reference_id or 'x'}",
        label=f"[{reference_id or 'x'}]",
        kind=kind,
        reference_id=reference_id,
        doc_name=doc_name,
        **extra,
    )


def output(
    *,
    skill: str = "knowledge_qa",
    status: str = "ok",
    answer: str = "答案",
    citations=None,
    evidence=None,
    elapsed_ms: float = 12.5,
) -> SkillOutput:
    return SkillOutput(
        status=status,
        answer=answer,
        citations=list(citations or []),
        evidence=list(evidence or []),
        trace={"skill": skill, "elapsed_ms": elapsed_ms},
    )


class FakeManager:
    def __init__(self, result) -> None:
        self.result = result
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RuntimeAnswerTests(unittest.TestCase):
    def test_knowledge_answer_and_references(self) -> None:
        manager = FakeManager(
            output(citations=[citation(reference_id="2", doc_name="b.md")])
        )
        runtime = production.SkillRuntime(manager)

        result = runtime.answer("问题")

        self.assertIsNotNone(result)
        self.assertEqual(result.response, "答案")
        self.assertEqual(result.skill, "knowledge_qa")
        self.assertEqual(result.response_time, 0.013)
        self.assertEqual(
            result.references,
            ({"reference_id": "2", "file_path": "b.md"},),
        )
        self.assertEqual(manager.requests[0].question, "问题")

    def test_error_output_falls_back(self) -> None:
        runtime = production.SkillRuntime(
            FakeManager(output(status="error", answer=""))
        )
        self.assertIsNone(runtime.answer("问题"))

    def test_empty_answer_falls_back(self) -> None:
        runtime = production.SkillRuntime(
            FakeManager(output(status="partial", answer="   "))
        )
        self.assertIsNone(runtime.answer("问题"))

    def test_manager_exception_falls_back(self) -> None:
        runtime = production.SkillRuntime(FakeManager(RuntimeError("boom")))
        self.assertIsNone(runtime.answer("问题"))

    def test_empty_question_falls_back(self) -> None:
        runtime = production.SkillRuntime(FakeManager(output()))
        self.assertIsNone(runtime.answer("   "))

    def test_knowledge_partial_without_references_is_usable(self) -> None:
        runtime = production.SkillRuntime(
            FakeManager(output(status="partial", answer="没有引用的答案"))
        )

        result = runtime.answer("问题")

        self.assertIsNotNone(result)
        self.assertEqual(result.response, "没有引用的答案")

    def test_image_without_evidence_falls_back(self) -> None:
        runtime = production.SkillRuntime(
            FakeManager(
                output(
                    skill=image_lookup.SKILL_NAME,
                    status="partial",
                    answer="没有找到匹配的图片资料。",
                )
            )
        )
        self.assertIsNone(runtime.answer("找图片"))

    def test_image_with_evidence_maps_image_fields(self) -> None:
        evidence = Evidence(
            evidence_id="image:img:f1:0",
            kind="image",
            source_id="img:f1:0",
            doc_name="照片.md",
            file_id="f1",
            image_id="img:f1:0",
        )
        image_citation = Citation(
            citation_id="image:img:f1:0",
            label="[img-1]",
            kind="image",
            doc_name="照片.md",
            file_id="f1",
            image_id="img:f1:0",
            caption="不应出现在引用里",
        )
        runtime = production.SkillRuntime(
            FakeManager(
                output(
                    skill=image_lookup.SKILL_NAME,
                    answer="找到 1 张相关图片：\n[img-1] 照片.md",
                    citations=[image_citation],
                    evidence=[evidence],
                )
            )
        )

        result = runtime.answer("找图片")

        self.assertIsNotNone(result)
        self.assertEqual(
            result.references,
            (
                {
                    "reference_id": "",
                    "file_path": "照片.md",
                    "kind": "image",
                    "label": "[img-1]",
                    "file_id": "f1",
                    "image_id": "img:f1:0",
                },
            ),
        )
        rendered = str(result.to_payload())
        self.assertNotIn("不应出现在引用里", rendered)
        self.assertNotIn("caption", rendered)

    def test_references_skip_empty_citation(self) -> None:
        self.assertEqual(
            production.references_from_output(
                output(citations=[citation(reference_id="", doc_name="")])
            ),
            [],
        )

    def test_non_skill_output_falls_back(self) -> None:
        runtime = production.SkillRuntime(FakeManager({"answer": "dict"}))
        self.assertIsNone(runtime.answer("问题"))


class BuildRuntimeTests(unittest.TestCase):
    def test_registers_only_expected_tools_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = production.build_runtime(
                audit=AuditLogger(Path(tmp))
            )
            try:
                self.assertEqual(
                    runtime.tool_manager.registry.names(),
                    ["image.search", "knowledge.answer", "knowledge.search"],
                )
                self.assertEqual(
                    runtime.manager.registry.names(),
                    ["image_lookup", "knowledge_qa"],
                )
                self.assertEqual(
                    tuple(runtime.tool_manager.policy.enabled_tools),
                    production.ENABLED_TOOLS,
                )
                self.assertTrue(runtime.tool_manager.policy.allowed_external)
                self.assertEqual(
                    runtime.tool_manager.policy.allowed_side_effects,
                    ("none", "read_local"),
                )
                self.assertEqual(runtime.manager.fallback, knowledge_qa.SKILL_NAME)
                self.assertEqual(
                    runtime.manager.router.fallback, knowledge_qa.SKILL_NAME
                )
            finally:
                runtime.close()

    def test_close_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = production.build_runtime(audit=AuditLogger(Path(tmp)))
            runtime.close()
            runtime.close()


class RoutingFlagTests(unittest.TestCase):
    def test_enabled_only_for_on(self) -> None:
        for value, expected in (("on", True), ("off", False), ("", False)):
            with self.subTest(value=value), mock.patch.object(
                production,
                "get_settings",
                return_value=SimpleNamespace(skill_routing=value),
            ):
                self.assertEqual(production.skill_routing_enabled(), expected)

    def test_config_exception_is_off(self) -> None:
        with mock.patch.object(production, "get_settings", side_effect=RuntimeError):
            self.assertFalse(production.skill_routing_enabled())


if __name__ == "__main__":
    unittest.main()
