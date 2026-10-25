"""P3B：本机生成版 `knowledge.answer` 工具的单测（不联网）。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.clients import llm
from app.core.errors import UpstreamError
from app.plugins.adapters import local_answer
from app.services import local_generation
from app.services.context_builder import BuiltContext, Citation


def context() -> BuiltContext:
    return BuiltContext(
        version=1,
        system_prompt="",
        context_text="",
        citation_map={
            "chunk:c1": Citation(
                citation_id="chunk:c1",
                label="[1]",
                kind="chunk",
                reference_id="1",
                doc_name="a.md",
            )
        },
        groups={},
        token_estimate=0,
        dropped=[],
    )


def answer() -> local_generation.LocalChatAnswer:
    return local_generation.LocalChatAnswer(
        response="本机答案 [1]",
        references=({"reference_id": "1", "file_path": "a.md"},),
        response_time=1.25,
        context=context(),
        citations=("[1]",),
        status="ok",
        timings_ms={"retrieve_ms": 4.0, "build_ms": 0.1, "llm_ms": 100.0, "total_ms": 104.1},
        model="qwen2.5:7b",
        usage={"eval_count": 3},
    )


class InvokeTests(unittest.TestCase):
    def test_success_maps_payload_and_evidence(self) -> None:
        with mock.patch.object(local_generation, "answer", return_value=answer()):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertTrue(result.ok)
        self.assertEqual(result.data["response"], "本机答案 [1]")
        self.assertEqual(result.data["response_time"], 1.25)
        self.assertEqual(result.data["references"][0]["reference_id"], "1")
        self.assertEqual(result.evidence[0].metadata["citation_label"], "[1]")
        self.assertEqual(result.usage["generation"], "local")
        self.assertEqual(result.usage["citation_count"], 1)

    def test_invalid_query_is_rejected_without_calling_generation(self) -> None:
        with mock.patch.object(local_generation, "answer") as generation:
            result = local_answer.TOOL.invoke(query="  ")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_input")
        generation.assert_not_called()

    def test_policy_failure_maps_to_tool_error(self) -> None:
        with mock.patch.object(
            local_generation,
            "answer",
            side_effect=local_generation.LocalGenerationError(
                "本机回答没有通过引用校验", kind="local_unverified"
            ),
        ):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "local_unverified")
        self.assertIn("引用校验", result.error.message)

    def test_upstream_error_maps_to_upstream_code(self) -> None:
        with mock.patch.object(
            local_generation,
            "answer",
            side_effect=UpstreamError("知识库服务不可用"),
        ):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "upstream_error")

    def test_llm_error_uses_safe_message(self) -> None:
        with mock.patch.object(
            local_generation,
            "answer",
            side_effect=llm.LLMError("连不上本机 Ollama", kind="llm_unavailable"),
        ):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "local_llm_error")
        self.assertEqual(result.error.message, "连不上本机 Ollama")

    def test_unknown_exception_does_not_leak_body(self) -> None:
        with mock.patch.object(
            local_generation,
            "answer",
            side_effect=RuntimeError("机密问题和答案"),
        ):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertFalse(result.ok)
        self.assertNotIn("机密问题和答案", result.error.message)
        self.assertIn("RuntimeError", result.error.message)


class HealthTests(unittest.TestCase):
    def test_model_present(self) -> None:
        with mock.patch.object(
            local_generation,
            "probe_model",
            return_value={
                "base_url": "http://localhost:11434",
                "model": "qwen2.5:7b",
                "model_present": True,
            },
        ):
            health = local_answer.TOOL.health()

        self.assertTrue(health.ok)
        self.assertIn("qwen2.5:7b", health.detail)

    def test_model_missing(self) -> None:
        with mock.patch.object(
            local_generation,
            "probe_model",
            return_value={
                "base_url": "http://localhost:11434",
                "model": "qwen2.5:7b",
                "model_present": False,
            },
        ):
            health = local_answer.TOOL.health()

        self.assertFalse(health.ok)
        self.assertIn("ollama pull", health.detail)

    def test_probe_error_does_not_raise(self) -> None:
        with mock.patch.object(
            local_generation,
            "probe_model",
            side_effect=llm.LLMError("未启用", kind="llm_disabled"),
        ):
            health = local_answer.TOOL.health()

        self.assertFalse(health.ok)
        self.assertEqual(health.detail, "未启用")


class SpecTests(unittest.TestCase):
    def test_tool_name_matches_upstream_contract(self) -> None:
        from app.plugins.adapters import knowledge

        self.assertEqual(local_answer.SPEC.name, knowledge.ANSWER_SPEC.name)
        self.assertTrue(local_answer.SPEC.allowed_for_skills)
        self.assertEqual(local_answer.SPEC.side_effect, "none")


if __name__ == "__main__":
    unittest.main()
