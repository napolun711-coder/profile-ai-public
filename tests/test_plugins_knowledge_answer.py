"""P2B：`knowledge.answer` 只读工具的离线单测。

全程用 mock 替换 `lightrag.query`：不联网、不读知识库、不写审计。
重点守住：强制 include_references、references → Evidence 的映射、错误码与脱敏。
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.clients import lightrag
from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError
from app.plugins import ToolContext, validate_spec
from app.plugins.adapters import knowledge

PAYLOAD = {
    "response": "答案 [1][2]",
    "references": [
        {"reference_id": "1", "file_path": "项目说明.md", "content": "片段一"},
        {"reference_id": "2", "file_path": "会议记录.md", "content": None},
    ],
    "response_time": 12.8,
}


class SpecTests(unittest.TestCase):
    def test_answer_spec_is_valid_and_not_builtin(self) -> None:
        validate_spec(knowledge.ANSWER_SPEC)

        self.assertEqual(knowledge.ANSWER_SPEC.name, "knowledge.answer")
        self.assertTrue(knowledge.ANSWER_SPEC.external)
        self.assertEqual(knowledge.ANSWER_SPEC.side_effect, "none")
        self.assertTrue(knowledge.ANSWER_SPEC.allowed_for_skills)
        self.assertEqual(knowledge.ANSWER_SPEC.retry.max_attempts, 1)

        from app.plugins import load_builtin_tools

        self.assertNotIn("knowledge.answer", load_builtin_tools().names())


class SuccessTests(unittest.TestCase):
    def test_forces_references_and_maps_evidence(self) -> None:
        with mock.patch.object(
            knowledge.lightrag, "query", return_value=PAYLOAD
        ) as query:
            result = knowledge.ANSWER_TOOL.invoke(
                ToolContext(), query="  知识库里有哪些项目？  "
            )

        query.assert_called_once_with("知识库里有哪些项目？", include_references=True)
        self.assertTrue(result.ok)
        self.assertIs(result.data, PAYLOAD)
        self.assertEqual(
            [item.evidence_id for item in result.evidence],
            ["reference:1", "reference:2"],
        )
        self.assertEqual(result.evidence[0].doc_name, "项目说明.md")
        self.assertEqual(result.evidence[0].text, "片段一")
        self.assertEqual(result.evidence[1].text, "")
        self.assertEqual(result.usage["answer_chars"], len("答案 [1][2]"))
        self.assertEqual(result.usage["reference_count"], 2)
        self.assertEqual(result.usage["response_time_s"], 12.8)

    def test_missing_references_yields_empty_evidence(self) -> None:
        with mock.patch.object(
            knowledge.lightrag, "query", return_value={"response": "答案"}
        ):
            result = knowledge.ANSWER_TOOL.invoke(None, query="知识库里有什么？")

        self.assertTrue(result.ok)
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.usage["reference_count"], 0)
        self.assertIsNone(result.usage["response_time_s"])

    def test_nested_data_references_are_supported(self) -> None:
        payload = {"response": "答案", "data": {"references": [{"reference_id": "9"}]}}

        evidence = knowledge.evidence_from_query_response(payload)

        self.assertEqual([item.evidence_id for item in evidence], ["reference:9"])

    def test_duplicate_reference_ids_are_deduped(self) -> None:
        payload = {
            "response": "答案",
            "references": [
                {"reference_id": "1", "file_path": "a.md"},
                {"reference_id": "1", "file_path": "b.md"},
            ],
        }

        evidence = knowledge.evidence_from_query_response(payload)

        self.assertEqual([item.evidence_id for item in evidence], ["reference:1"])
        self.assertEqual(evidence[0].doc_name, "a.md")

    def test_non_dict_references_are_skipped(self) -> None:
        payload = {"response": "答案", "references": ["bad", None, {"reference_id": "2"}]}

        evidence = knowledge.evidence_from_query_response(payload)

        self.assertEqual([item.evidence_id for item in evidence], ["reference:2"])
        self.assertEqual(knowledge.evidence_from_query_response(["bad"]), [])


class InputTests(unittest.TestCase):
    def test_invalid_query_is_rejected_before_upstream_call(self) -> None:
        with mock.patch.object(knowledge.lightrag, "query") as query:
            for value in ("ab", "  a  ", "", 123, None):
                with self.subTest(value=value):
                    result = knowledge.ANSWER_TOOL.invoke(None, query=value)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error.code, "invalid_input")
            query.assert_not_called()


class ErrorTests(unittest.TestCase):
    def test_upstream_error_is_mapped_and_retryable(self) -> None:
        failure = UpstreamError(
            "知识库服务返回错误（HTTP 503）",
            upstream_status=503,
            upstream_body="上游敏感片段",
        )
        with mock.patch.object(knowledge.lightrag, "query", side_effect=failure):
            result = knowledge.ANSWER_TOOL.invoke(None, query="知识库问题")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "upstream_error")
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.error.upstream_status, 503)
        self.assertEqual(result.error.detail, "上游敏感片段")
        self.assertNotIn("上游敏感片段", result.error.message)

    def test_timeout_is_retryable(self) -> None:
        failure = UpstreamError(
            "知识库查询超时", kind=UPSTREAM_TIMEOUT, status_code=504
        )
        with mock.patch.object(knowledge.lightrag, "query", side_effect=failure):
            result = knowledge.ANSWER_TOOL.invoke(None, query="知识库问题")

        self.assertEqual(result.error.code, "upstream_error")
        self.assertTrue(result.error.retryable)

    def test_internal_exception_keeps_only_type_in_message(self) -> None:
        secret = "内部异常里的敏感正文"
        with mock.patch.object(
            knowledge.lightrag, "query", side_effect=RuntimeError(secret)
        ):
            result = knowledge.ANSWER_TOOL.invoke(None, query="知识库问题")

        self.assertEqual(result.error.code, "internal_error")
        self.assertIn("RuntimeError", result.error.message)
        self.assertNotIn(secret, result.error.message)
        self.assertEqual(result.error.detail, secret)


class HealthTests(unittest.TestCase):
    def test_health_maps_probe_result(self) -> None:
        with mock.patch.object(
            knowledge.lightrag,
            "probe_health",
            return_value={"ok": True, "status_code": 200, "detail": ""},
        ):
            health = knowledge.ANSWER_TOOL.health()

        self.assertTrue(health.ok)
        self.assertIn("200", health.detail)

    def test_health_failure_is_structured(self) -> None:
        with mock.patch.object(
            knowledge.lightrag, "probe_health", side_effect=RuntimeError("boom")
        ):
            health = knowledge.ANSWER_TOOL.health()

        self.assertFalse(health.ok)
        self.assertIn("RuntimeError", health.detail)


if __name__ == "__main__":
    unittest.main()
