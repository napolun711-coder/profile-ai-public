"""P3B：回答生成方式（默认上游 / 本机 local）的离线单测。"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.config import Settings, _build_settings
from app.plugins import Evidence
from app.routers import chat as chat_router
from app.services import local_chain, local_generation
from app.services.context_builder import BuiltContext, Citation
from app.skills import production
from app.skills.builtin import knowledge_qa


def built_context() -> BuiltContext:
    citations = {
        "chunk:c1": Citation(
            citation_id="chunk:c1",
            label="[1]",
            kind="chunk",
            reference_id="1",
            doc_name="a.md",
        ),
        "chunk:c2": Citation(
            citation_id="chunk:c2",
            label="[1]",
            kind="chunk",
            reference_id="1",
            doc_name="a.md",
        ),
        "image:img1": Citation(
            citation_id="image:img1",
            label="[img-1]",
            kind="image",
            doc_name="照片.md",
            file_id="f1",
            image_id="img:f1:0",
        ),
        "entity:e": Citation(
            citation_id="entity:e",
            label="[entity:e]",
            kind="entity",
        ),
    }
    return BuiltContext(
        version=1,
        system_prompt="",
        context_text="",
        citation_map=citations,
        groups={},
        token_estimate=0,
        dropped=[],
    )


def local_answer(
    *,
    answer: str = "结论 [1]。",
    cited=("[1]",),
    unknown=(),
    timings=None,
) -> local_chain.LocalAnswer:
    return local_chain.LocalAnswer(
        question="问题",
        answer=answer,
        context=built_context(),
        messages=[],
        cited_labels=list(cited),
        unknown_labels=list(unknown),
        timings_ms=timings
        or {"retrieve_ms": 4.0, "build_ms": 0.1, "llm_ms": 100.0, "total_ms": 104.1},
        model="qwen2.5:7b",
        usage={"eval_count": 3},
    )


class ConfigTests(unittest.TestCase):
    def test_default_is_upstream(self) -> None:
        self.assertEqual(Settings().generation_mode, "upstream")

    def test_env_values(self) -> None:
        with mock.patch.dict(os.environ, {"GENERATION_MODE": "local"}, clear=False):
            self.assertEqual(_build_settings().generation_mode, "upstream")
        with mock.patch.dict(os.environ, {"GENERATION_MODE": "UPSTREAM"}, clear=False):
            self.assertEqual(_build_settings().generation_mode, "upstream")

    def test_invalid_falls_back_to_upstream(self) -> None:
        with mock.patch.dict(os.environ, {"GENERATION_MODE": "cloud"}, clear=False):
            self.assertEqual(_build_settings().generation_mode, "upstream")


class GenerationModeTests(unittest.TestCase):
    def test_generation_mode_reads_settings_and_never_raises(self) -> None:
        self.assertEqual(
            local_generation.generation_mode(Settings(generation_mode="local")),
            "local",
        )
        self.assertEqual(
            local_generation.generation_mode(Settings(generation_mode="bogus")),
            "upstream",
        )
        with mock.patch.object(
            local_generation, "get_settings", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(local_generation.generation_mode(), "upstream")

    def test_upstream_mode_does_not_call_chain(self) -> None:
        with mock.patch.object(local_chain, "answer") as chain:
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                local_generation.answer("问题", settings=Settings(generation_mode="upstream"))

        self.assertEqual(caught.exception.kind, "local_off")
        chain.assert_not_called()

    def test_local_mode_maps_payload(self) -> None:
        with mock.patch.object(local_chain, "answer", return_value=local_answer()):
            result = local_generation.answer(
                "问题", settings=Settings(generation_mode="local")
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.response_time, 0.104)
        self.assertEqual(result.to_payload()["response"], "结论 [1]。")
        self.assertEqual(
            [item["reference_id"] for item in result.references], ["1", ""]
        )
        # 同一 reference_id 的多个 chunk 只保留一条；实体不进入前端 references。
        self.assertEqual(len(result.references), 2)
        image = result.references[1]
        self.assertEqual(image["kind"], "image")
        self.assertEqual(image["label"], "[img-1]")
        self.assertEqual(image["file_id"], "f1")
        self.assertEqual(image["image_id"], "img:f1:0")

    def test_unverified_answer_raises(self) -> None:
        with mock.patch.object(
            local_chain,
            "answer",
            return_value=local_answer(answer="一段总结。", cited=()),
        ):
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                local_generation.answer(
                    "问题", settings=Settings(generation_mode="local")
                )

        self.assertEqual(caught.exception.kind, "local_unverified")

    def test_unknown_citation_raises(self) -> None:
        with mock.patch.object(
            local_chain,
            "answer",
            return_value=local_answer(answer="结论 [9]。", cited=("[9]",), unknown=("[9]",)),
        ):
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                local_generation.answer(
                    "问题", settings=Settings(generation_mode="local")
                )

        self.assertEqual(caught.exception.kind, "local_unverified")

    def test_refusal_is_returned(self) -> None:
        with mock.patch.object(
            local_chain,
            "answer",
            return_value=local_answer(answer="资料中没有足够信息。", cited=()),
        ):
            result = local_generation.answer(
                "问题", settings=Settings(generation_mode="local")
            )

        self.assertEqual(result.status, "refusal")
        self.assertIn("资料中没有足够信息", result.response)

    def test_evidence_carries_exact_labels(self) -> None:
        evidence = local_generation.evidence_from_context(built_context())
        labels = {item.metadata.get("citation_label") for item in evidence}

        self.assertEqual(labels, {"[1]", "[img-1]"})
        image = [item for item in evidence if item.kind == "image"][0]
        self.assertEqual(image.file_id, "f1")
        self.assertEqual(image.image_id, "img:f1:0")


class RouterTests(unittest.TestCase):
    def test_local_answer_skips_when_upstream_or_skill_routing(self) -> None:
        with mock.patch.object(chat_router, "_generation_mode", return_value="upstream"):
            self.assertIsNone(chat_router._local_answer("问题"))
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(chat_router, "_skill_routing_on", return_value=True):
            self.assertIsNone(chat_router._local_answer("问题"))

    def test_local_failure_returns_none(self) -> None:
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=False
        ), mock.patch.object(
            local_generation, "answer", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(chat_router._local_answer("问题"))

    def test_chat_prefers_local_answer(self) -> None:
        payload = {
            "response": "本机答案 [1]",
            "references": [{"reference_id": "1", "file_path": "a.md"}],
            "response_time": 1.0,
        }
        fake = SimpleNamespace(to_payload=lambda: payload)
        with mock.patch.object(
            chat_router, "_local_answer", return_value=fake
        ), mock.patch.object(chat_router, "_skill_answer") as skill, mock.patch.object(
            chat_router.lightrag, "query"
        ) as query:
            result = chat_router.chat(QuestionStub("问题"))

        self.assertEqual(result, payload)
        skill.assert_not_called()
        query.assert_not_called()

    def test_chat_falls_back_to_upstream_when_local_fails(self) -> None:
        with mock.patch.object(
            chat_router, "_local_answer", return_value=None
        ), mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query", return_value={"response": "上游"}
        ) as query:
            result = chat_router.chat(QuestionStub("问题"))

        self.assertEqual(result, {"response": "上游"})
        query.assert_called_once_with("问题")

    def test_stream_local_events(self) -> None:
        fake = SimpleNamespace(
            references=({"reference_id": "1", "file_path": "a.md"},),
            response="本机答案 [1]",
            response_time=1.5,
        )

        def fake_stream(question):
            yield {"type": "references", "items": [{"reference_id": "1"}]}
            yield {"type": "chunk", "text": "本机答案 "}
            yield {"type": "chunk", "text": "[1]"}
            yield {"type": "_answer", "answer": fake}

        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=False
        ), mock.patch.object(
            local_generation, "iter_stream", fake_stream
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(
            [event["type"] for event in events],
            ["references", "chunk", "chunk", "done", "end"],
        )
        self.assertEqual(
            "".join(event["text"] for event in events if event["type"] == "chunk"),
            "本机答案 [1]",
        )
        self.assertEqual(events[-2]["response_time"], 1.5)

    def test_stream_falls_back_to_upstream(self) -> None:
        with mock.patch.object(
            chat_router, "_local_answer", return_value=None
        ), mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query_stream", return_value=[{"response": "上游"}]
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(events[0], {"type": "chunk", "text": "上游"})
        self.assertEqual(events[-1], {"type": "end"})


class QuestionStub:
    """轻量请求体；避免依赖 pydantic 版本差异。"""

    def __init__(self, message: str) -> None:
        self.message = message


class ToolSelectionTests(unittest.TestCase):
    def test_upstream_selects_upstream_tool(self) -> None:
        with mock.patch.object(
            production,
            "get_settings",
            return_value=SimpleNamespace(generation_mode="upstream"),
        ):
            from app.plugins.adapters import knowledge

            self.assertIs(production._answer_tool(), knowledge.ANSWER_TOOL)

    def test_local_selects_local_tool(self) -> None:
        from app.plugins.adapters import local_answer as local_tool

        with mock.patch.object(
            production,
            "get_settings",
            return_value=SimpleNamespace(generation_mode="local"),
        ):
            self.assertIs(production._answer_tool(), local_tool.TOOL)


class EndToEndLocalToolTests(unittest.TestCase):
    def test_local_tool_through_real_manager(self) -> None:
        import tempfile
        from pathlib import Path

        from app.plugins.audit import AuditLogger

        fake = local_generation.LocalChatAnswer(
            response="本机答案 [img-1]",
            references=(
                {
                    "reference_id": "",
                    "file_path": "照片.md",
                    "kind": "image",
                    "label": "[img-1]",
                    "file_id": "f1",
                    "image_id": "img:f1:0",
                },
            ),
            response_time=1.0,
            context=built_context(),
            citations=("[img-1]",),
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            production,
            "get_settings",
            return_value=SimpleNamespace(generation_mode="local", skill_routing="on"),
        ), mock.patch.object(local_generation, "answer", return_value=fake):
            runtime = production.build_runtime(audit=AuditLogger(Path(tmp)))
            try:
                result = runtime.answer("知识库里有哪些项目")
            finally:
                runtime.close()

        self.assertIsNotNone(result)
        self.assertEqual(result.response, "本机答案 [img-1]")
        images = [item for item in result.references if item.get("kind") == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["label"], "[img-1]")
        self.assertEqual(images[0]["image_id"], "img:f1:0")


class SettingsProbeTests(unittest.TestCase):
    def _run(self, mode):
        from app.routers import settings as settings_router

        data = {"app": {"generation_mode": mode}}
        check = mock.Mock(
            return_value={
                "name": "本机生成模型（Ollama）",
                "ok": True,
                "detail": "已就绪",
            }
        )
        with mock.patch.object(
            settings_router.user_settings, "load", return_value=data
        ), mock.patch.object(
            settings_router, "_effective", return_value={}
        ), mock.patch.object(
            settings_router.lightrag_client,
            "probe_health",
            return_value={"ok": True, "url": "x"},
        ), mock.patch.object(
            settings_router, "_local_generation_check", check
        ), mock.patch.object(
            settings_router,
            "_ocr_check",
            return_value={"name": "OCR", "ok": True, "detail": ""},
        ):
            result = settings_router.test_connections()
        return check, result

    def test_local_mode_setting_cannot_probe_or_enable_ollama(self) -> None:
        check, result = self._run("local")

        check.assert_not_called()
        self.assertFalse(
            any(item["name"] == "本机生成模型（Ollama）" for item in result["checks"])
        )

    def test_upstream_mode_does_not_probe_ollama(self) -> None:
        check, result = self._run("upstream")

        check.assert_not_called()
        self.assertFalse(
            any(item["name"] == "本机生成模型（Ollama）" for item in result["checks"])
        )


class CitationPassthroughTests(unittest.TestCase):
    def test_exact_label_and_image_fields_are_preserved(self) -> None:
        evidence = Evidence(
            evidence_id="image:1",
            kind="image",
            source_id="",
            doc_name="照片.md",
            file_id="f1",
            image_id="img:f1:0",
            metadata={"citation_label": "[img-1]"},
        )

        citations = knowledge_qa.citations_from_evidence([evidence])

        self.assertEqual(citations[0].label, "[img-1]")
        self.assertEqual(citations[0].file_id, "f1")
        self.assertEqual(citations[0].image_id, "img:f1:0")

    def test_label_without_brackets_is_normalized(self) -> None:
        evidence = Evidence(
            evidence_id="document:1",
            kind="document",
            doc_name="a.md",
            metadata={"citation_label": "doc-1"},
        )

        self.assertEqual(
            knowledge_qa.citations_from_evidence([evidence])[0].label, "[doc-1]"
        )


if __name__ == "__main__":
    unittest.main()
