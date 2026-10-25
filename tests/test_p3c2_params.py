"""P3C.2：参数配置、done_reason、校验元数据接线的离线单测。"""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from app.clients import llm
from app.core.config import Settings, _build_settings
from app.plugins.adapters import local_answer
from app.routers import chat as chat_router
from app.services import local_chain, local_generation
from app.services.context_builder import BuiltContext, Citation
from app.skills import production
from app.skills.contracts import SkillOutput


def built_context() -> BuiltContext:
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


def chain_answer(
    *,
    answer: str = "结论 [1]。",
    usage=None,
) -> local_chain.LocalAnswer:
    return local_chain.LocalAnswer(
        question="问题",
        answer=answer,
        context=built_context(),
        messages=[],
        cited_labels=["[1]"] if "[1]" in answer else [],
        unknown_labels=[],
        timings_ms={"total_ms": 100.0},
        model="qwen2.5:7b",
        usage=usage or {"done_reason": "stop"},
    )


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.status_code = 200
        self.closed = False

    def json(self):
        return self.payload

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ConfigTests(unittest.TestCase):
    def test_defaults_keep_p3a_behaviour(self) -> None:
        settings = Settings()

        self.assertEqual(settings.llm_temperature, 0.0)
        self.assertEqual(settings.llm_keep_alive, "5m")
        self.assertEqual(settings.llm_context_budget, 4000)
        self.assertEqual(settings.llm_num_ctx, 8192)
        self.assertEqual(settings.llm_num_predict, 512)

    def test_env_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_TEMPERATURE": "0.2",
                "LOCAL_LLM_KEEP_ALIVE": "30m",
                "LOCAL_LLM_CONTEXT_BUDGET": "3000",
                "LOCAL_LLM_NUM_CTX": "4096",
                "LOCAL_LLM_NUM_PREDICT": "768",
            },
            clear=False,
        ):
            settings = _build_settings()

        self.assertEqual(settings.llm_temperature, 0.2)
        self.assertEqual(settings.llm_keep_alive, "30m")
        self.assertEqual(settings.llm_context_budget, 3000)
        self.assertEqual(settings.llm_num_ctx, 4096)
        self.assertEqual(settings.llm_num_predict, 768)

    def test_invalid_and_out_of_range_values_fall_back(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_TEMPERATURE": "9",
                "LOCAL_LLM_KEEP_ALIVE": "   ",
                "LOCAL_LLM_CONTEXT_BUDGET": "10",
            },
            clear=False,
        ):
            settings = _build_settings()

        self.assertEqual(settings.llm_temperature, 2.0)
        self.assertEqual(settings.llm_keep_alive, "")
        self.assertEqual(settings.llm_context_budget, 500)

        with mock.patch.dict(
            os.environ,
            {"LOCAL_LLM_TEMPERATURE": "x", "LOCAL_LLM_CONTEXT_BUDGET": "x"},
            clear=False,
        ):
            settings = _build_settings()

        self.assertEqual(settings.llm_temperature, 0.0)
        self.assertEqual(settings.llm_context_budget, 4000)

    def test_log_dict_has_no_secret(self) -> None:
        data = Settings().to_log_dict()

        self.assertEqual(data["llm_temperature"], 0.0)
        self.assertEqual(data["llm_keep_alive"], "5m")
        self.assertEqual(data["llm_context_budget"], 4000)


class LLMPayloadTests(unittest.TestCase):
    def test_chat_sends_temperature_keep_alive_and_options(self) -> None:
        response = FakeResponse(
            {"message": {"content": "答案"}, "model": "qwen2.5:7b"}
        )
        session = FakeSession(response)
        settings = Settings(
            llm_enabled=True,
            llm_num_ctx=4096,
            llm_num_predict=768,
            llm_temperature=0.3,
            llm_keep_alive="30m",
        )

        llm.chat(
            [{"role": "user", "content": "问题"}],
            settings=settings,
            session=session,
        )

        payload = session.calls[0][1]["json"]
        self.assertEqual(payload["keep_alive"], "30m")
        self.assertEqual(payload["options"]["temperature"], 0.3)
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(payload["options"]["num_predict"], 768)
        self.assertEqual(payload["options"]["seed"], llm.DEFAULT_SEED)

    def test_empty_keep_alive_is_omitted(self) -> None:
        response = FakeResponse(
            {"message": {"content": "答案"}, "model": "qwen2.5:7b"}
        )
        session = FakeSession(response)

        llm.chat(
            [{"role": "user", "content": "问题"}],
            settings=Settings(llm_enabled=True, llm_keep_alive=""),
            session=session,
        )

        self.assertNotIn("keep_alive", session.calls[0][1]["json"])

    def test_usage_records_done_reason(self) -> None:
        usage = llm._usage_from(
            {"done_reason": "length", "eval_count": 512, "prompt_eval_count": 100}
        )

        self.assertEqual(usage["done_reason"], "length")
        self.assertEqual(usage["eval_count"], 512)
        self.assertEqual(usage["prompt_eval_count"], 100)


class GenerationValidationTests(unittest.TestCase):
    def test_truncated_answer_raises(self) -> None:
        result = chain_answer(usage={"done_reason": "length"})
        with mock.patch.object(local_chain, "answer", return_value=result):
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                local_generation.answer(
                    "问题", settings=Settings(generation_mode="local")
                )

        self.assertEqual(caught.exception.kind, "local_truncated")

    def test_warnings_are_attached_without_failing(self) -> None:
        result = chain_answer(answer="好 [1]。", usage={"done_reason": "stop"})
        with mock.patch.object(local_chain, "answer", return_value=result):
            answer = local_generation.answer(
                "问题", settings=Settings(generation_mode="local")
            )

        self.assertTrue(answer.verified)
        self.assertIn("too_short", answer.content_warnings)
        self.assertEqual(answer.to_payload()["response"], "好 [1]。")

    def test_context_budget_is_passed_from_settings(self) -> None:
        result = chain_answer()
        with mock.patch.object(
            local_chain, "answer", return_value=result
        ) as chain:
            local_generation.answer(
                "问题",
                settings=Settings(generation_mode="local", llm_context_budget=3000),
            )

        budget = chain.call_args.kwargs["budget"]
        self.assertEqual(budget.total, 3000)

    def test_done_event_contains_only_safe_metadata(self) -> None:
        answer = local_generation.LocalChatAnswer(
            response="答案 [1]",
            references=(),
            response_time=0.5,
            context=built_context(),
            verified=False,
            content_warnings=("unsupported_number",),
            usage={
                "prompt_eval_count": 10,
                "eval_count": 20,
                "done_reason": "stop",
                "secret": "不该出现",
            },
        )

        event = chat_router._done_event(answer, include_content_meta=True)

        self.assertEqual(event["verified"], False)
        self.assertEqual(event["warnings"], ["unsupported_number"])
        self.assertEqual(event["usage"]["prompt_eval_count"], 10)
        self.assertEqual(event["usage"]["done_reason"], "stop")
        self.assertNotIn("secret", event["usage"])
        self.assertNotIn("不该出现", json.dumps(event, ensure_ascii=False))

    def test_done_event_defaults_for_legacy_answers(self) -> None:
        event = chat_router._done_event(
            SimpleNamespace(response_time=1.0), include_content_meta=True
        )

        # 没有校验结果时不能谎报 verified=True（图片技能等非本机路径）。
        self.assertNotIn("verified", event)
        self.assertEqual(event["warnings"], [])
        self.assertNotIn("usage", event)

    def test_done_event_reports_verified_only_when_validated(self) -> None:
        answer = SimpleNamespace(
            response_time=0.5, verified=None, warnings=(), usage={}
        )
        event = chat_router._done_event(answer, include_content_meta=True)
        self.assertNotIn("verified", event)


class ToolMetadataTests(unittest.TestCase):
    def _answer(self) -> local_generation.LocalChatAnswer:
        return local_generation.LocalChatAnswer(
            response="答案 [1]",
            references=({"reference_id": "1", "file_path": "a.md"},),
            response_time=0.5,
            context=built_context(),
            citations=("[1]",),
            verified=False,
            content_warnings=("low_citation_coverage",),
            usage={
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 34,
            },
        )

    def test_tool_data_and_usage_carry_validation_metadata(self) -> None:
        with mock.patch.object(local_generation, "answer", return_value=self._answer()):
            result = local_answer.TOOL.invoke(query="知识库里有什么")

        self.assertTrue(result.ok)
        self.assertFalse(result.data["verified"])
        self.assertEqual(result.data["warnings"], ["low_citation_coverage"])
        self.assertFalse(result.usage["verified"])
        self.assertEqual(
            result.usage["content_warning_codes"], ["low_citation_coverage"]
        )
        self.assertEqual(result.usage["done_reason"], "stop")
        self.assertEqual(result.usage["prompt_eval_count"], 12)

    def test_skill_runtime_reads_validation_metadata(self) -> None:
        output = SkillOutput(
            status="ok",
            answer="答案 [1]",
            usage={
                "verified": False,
                "content_warning_codes": ["too_short"],
                "done_reason": "length",
                "prompt_eval_count": 5,
                "unexpected": "drop",
            },
            trace={"skill": "knowledge_qa", "elapsed_ms": 10.0},
        )
        runtime = production.SkillRuntime(manager=mock.Mock())

        answer = runtime._to_answer(output, 0.0)

        self.assertIsNotNone(answer)
        self.assertFalse(answer.verified)
        self.assertEqual(answer.warnings, ("too_short",))
        self.assertEqual(answer.usage["done_reason"], "length")
        self.assertEqual(answer.usage["prompt_eval_count"], 5)
        self.assertNotIn("unexpected", answer.usage)


if __name__ == "__main__":
    unittest.main()
