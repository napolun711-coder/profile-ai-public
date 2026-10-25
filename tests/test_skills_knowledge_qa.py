"""P2B：`knowledge_qa` 技能的离线单测。

用假 Context/假 ToolManager 替代真实工具：不联网、不写审计。
覆盖规则评分、引用映射、partial、错误收敛、trace 脱敏与 Manager 集成。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.plugins import Evidence, ToolError, ToolResult
from app.skills import SkillInput, SkillManager, SkillRegistry
from app.skills.builtin import knowledge_qa


def answer_result(*, response: str = "答案 [1]", evidence=None) -> ToolResult:
    data = {
        "response": response,
        "references": [
            {"reference_id": "1", "file_path": "项目说明.md"},
        ],
        "response_time": 12.8,
    }
    if evidence is None:
        evidence = [
            Evidence(
                evidence_id="reference:1",
                kind="reference",
                source_id="1",
                doc_name="项目说明.md",
                text="证据正文",
                metadata={"reference_id": "1"},
            )
        ]
    return ToolResult.success(data, evidence=list(evidence))


class FakeTrace:
    def __init__(self) -> None:
        self.detail = {}


class FakeContext:
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls = []
        self.trace = FakeTrace()

    def call_tool(self, name, *, args=None, tool_ctx=None):
        self.calls.append({"name": name, "args": args})
        return self.result


class CanHandleTests(unittest.TestCase):
    def test_empty_question_scores_zero(self) -> None:
        self.assertEqual(knowledge_qa.SKILL.can_handle(SkillInput("   ")), 0.0)

    def test_image_only_cue_scores_below_threshold(self) -> None:
        self.assertEqual(
            knowledge_qa.SKILL.can_handle(SkillInput("这张图里有什么？")),
            knowledge_qa.IMAGE_ONLY_SCORE,
        )

    def test_knowledge_cue_scores_high(self) -> None:
        self.assertEqual(
            knowledge_qa.SKILL.can_handle(SkillInput("知识库里有哪些项目？")),
            knowledge_qa.KNOWLEDGE_SCORE,
        )

    def test_default_question_scores_default(self) -> None:
        self.assertEqual(
            knowledge_qa.SKILL.can_handle(SkillInput("帮我看看这个")),
            knowledge_qa.DEFAULT_SCORE,
        )

    def test_both_cues_prefer_knowledge(self) -> None:
        self.assertEqual(
            knowledge_qa.SKILL.can_handle(
                SkillInput("知识库里的这张图讲了什么？")
            ),
            knowledge_qa.KNOWLEDGE_SCORE,
        )

    def test_non_string_question_scores_zero(self) -> None:
        request = SimpleNamespace(question=None)
        self.assertEqual(knowledge_qa.SKILL.can_handle(request), 0.0)


class ExecuteTests(unittest.TestCase):
    def test_success_maps_citations_and_counts(self) -> None:
        ctx = FakeContext(answer_result())

        output = knowledge_qa.SKILL.execute(SkillInput("知识库里有哪些项目？"), ctx)

        self.assertEqual(
            ctx.calls,
            [{"name": "knowledge.answer", "args": {"query": "知识库里有哪些项目？"}}],
        )
        self.assertEqual(output.status, "ok")
        self.assertEqual(output.answer, "答案 [1]")
        self.assertEqual(len(output.evidence), 1)
        self.assertEqual(len(output.citations), 1)
        citation = output.citations[0]
        self.assertEqual(citation.citation_id, "reference:1")
        self.assertEqual(citation.label, "[1]")
        self.assertEqual(citation.kind, "reference")
        self.assertEqual(citation.reference_id, "1")
        self.assertEqual(citation.doc_name, "项目说明.md")
        self.assertEqual(output.usage["answer_chars"], len("答案 [1]"))
        self.assertEqual(output.usage["reference_count"], 1)
        self.assertEqual(output.usage["citation_count"], 1)
        self.assertEqual(output.usage["response_time_s"], 12.8)
        self.assertEqual(ctx.trace.detail["reference_count"], 1)
        self.assertFalse(ctx.trace.detail["no_references"])

    def test_no_references_marks_partial(self) -> None:
        ctx = FakeContext(answer_result(evidence=[]))

        output = knowledge_qa.SKILL.execute(SkillInput("知识库问题"), ctx)

        self.assertEqual(output.status, "partial")
        self.assertEqual(output.citations, [])
        self.assertEqual(output.usage["reference_count"], 0)
        self.assertTrue(ctx.trace.detail["no_references"])

    def test_empty_answer_is_error(self) -> None:
        ctx = FakeContext(answer_result(response="   "))

        output = knowledge_qa.SKILL.execute(SkillInput("知识库问题"), ctx)

        self.assertEqual(output.status, "error")
        self.assertEqual(output.error.code, knowledge_qa.ERROR_EMPTY_ANSWER)

    def test_non_dict_data_is_invalid_response(self) -> None:
        ctx = FakeContext(ToolResult.success(["not-a-dict"]))

        output = knowledge_qa.SKILL.execute(SkillInput("知识库问题"), ctx)

        self.assertEqual(output.status, "error")
        self.assertEqual(output.error.code, knowledge_qa.ERROR_INVALID_RESPONSE)

    def test_tool_failure_is_mapped_without_detail(self) -> None:
        ctx = FakeContext(
            ToolResult.failure(
                ToolError(
                    code="upstream_error",
                    message="知识库服务返回错误（HTTP 503）",
                    retryable=True,
                    detail="上游敏感片段",
                )
            )
        )

        output = knowledge_qa.SKILL.execute(SkillInput("知识库问题"), ctx)

        self.assertEqual(output.status, "error")
        self.assertEqual(output.error.code, "upstream_error")
        self.assertTrue(output.error.retryable)
        self.assertNotIn("上游敏感片段", json.dumps(output.to_dict(), ensure_ascii=False))

    def test_citation_without_reference_id_uses_stable_fallback(self) -> None:
        ctx = FakeContext(
            answer_result(
                evidence=[
                    Evidence(
                        evidence_id="reference:0",
                        kind="reference",
                        source_id="",
                        doc_name="",
                    )
                ]
            )
        )

        output = knowledge_qa.SKILL.execute(SkillInput("知识库问题"), ctx)

        self.assertEqual(output.citations[0].label, "[reference:0]")
        self.assertEqual(output.citations[0].reference_id, "")

    def test_trace_detail_never_contains_bodies(self) -> None:
        secret_question = "机密问题正文"
        secret_answer = "机密答案正文 [1]"
        secret_evidence = "机密证据正文"
        ctx = FakeContext(
            answer_result(
                response=secret_answer,
                evidence=[
                    Evidence(
                        evidence_id="reference:1",
                        kind="reference",
                        source_id="1",
                        doc_name="公开文件名.md",
                        text=secret_evidence,
                        metadata={"reference_id": "1"},
                    )
                ],
            )
        )

        knowledge_qa.SKILL.execute(SkillInput(secret_question), ctx)
        rendered = json.dumps(ctx.trace.detail, ensure_ascii=False)

        for label, secret in (
            ("question", secret_question),
            ("answer", secret_answer),
            ("evidence", secret_evidence),
        ):
            with self.subTest(label=label):
                self.assertNotIn(secret, rendered)


class FakeToolManager:
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls = []

    def call(self, name, *, args=None, ctx=None, caller="", declared=()):
        self.calls.append(
            {
                "name": name,
                "args": args,
                "caller": caller,
                "declared": declared,
            }
        )
        return self.result


class ManagerIntegrationTests(unittest.TestCase):
    def test_manager_routes_and_traces_a_successful_answer(self) -> None:
        registry = SkillRegistry()
        registry.register(knowledge_qa.SKILL)
        tools = FakeToolManager(answer_result())
        manager = SkillManager(registry, tools=tools, fallback="knowledge_qa")

        output = manager.run(SkillInput("知识库里有哪些项目？"))

        self.assertEqual(output.status, "ok")
        self.assertEqual(output.trace["route"], "rule")
        self.assertEqual(output.trace["skill"], "knowledge_qa")
        self.assertEqual(output.trace["steps"], 1)
        self.assertEqual(len(output.trace["tool_calls"]), 1)
        self.assertEqual(output.trace["tool_calls"][0]["tool"], "knowledge.answer")
        self.assertEqual(output.trace["tool_calls"][0]["args_keys"], ["query"])
        self.assertEqual(tools.calls[0]["caller"], "skill")
        self.assertEqual(tools.calls[0]["declared"], ("knowledge.answer",))
        self.assertEqual(output.trace["detail"]["reference_count"], 1)

        rendered = json.dumps(output.trace, ensure_ascii=False)
        self.assertNotIn("知识库里有哪些项目？", rendered)
        self.assertNotIn("答案 [1]", rendered)
        self.assertNotIn("证据正文", rendered)


if __name__ == "__main__":
    unittest.main()
