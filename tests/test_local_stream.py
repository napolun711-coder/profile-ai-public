"""P3C：真流式客户端、链路、门面、工具与路由的离线单测。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.clients import llm
from app.core.config import Settings
from app.core.errors import UpstreamError
from app.plugins.adapters import local_answer
from app.plugins.contracts import ToolContext
from app.plugins.audit import AuditLogger
from app.routers import chat as chat_router
from app.services import local_chain, local_generation
from app.services.context_builder import BuiltContext, Citation
from app.skills import production


# ============================================================
# 公共夹具
# ============================================================


def query_data() -> dict:
    return {
        "data": {
            "chunks": [
                {
                    "chunk_id": "c1",
                    "reference_id": "1",
                    "file_path": "a.md",
                    "content": "第一段",
                }
            ],
            "entities": [],
            "relationships": [],
            "references": [],
        }
    }


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
            ),
            "image:img1": Citation(
                citation_id="image:img1",
                label="[img-1]",
                kind="image",
                doc_name="照片.md",
                file_id="f1",
                image_id="img:f1:0",
            ),
        },
        groups={},
        token_estimate=0,
        dropped=[],
    )


def stream_chunks(*contents: str):
    for text in contents:
        yield llm.LLMStreamChunk(content=text, model="qwen2.5:7b")
    yield llm.LLMStreamChunk(
        content="", model="qwen2.5:7b", usage={"eval_count": 2}, done=True
    )


def local_answer_result(answer: str = "结论 [1]。", cited=("[1]",), unknown=()):
    return local_chain.LocalAnswer(
        question="问题",
        answer=answer,
        context=built_context(),
        messages=[],
        cited_labels=list(cited),
        unknown_labels=list(unknown),
        timings_ms={
            "retrieve_ms": 4.0,
            "build_ms": 0.2,
            "llm_ms": 120.0,
            "first_token_ms": 30.0,
            "total_ms": 124.2,
        },
        model="qwen2.5:7b",
        usage={"eval_count": 2},
    )


class FakeResponse:
    def __init__(self, lines, status_code: int = 200) -> None:
        self.lines = lines
        self.status_code = status_code
        self.closed = False

    def iter_lines(self):
        for line in self.lines:
            yield line

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def local_settings(**overrides):
    values = dict(
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:11434",
        llm_model="qwen2.5:7b",
        llm_timeout=60,
        llm_connect_timeout=5,
    )
    values.update(overrides)
    return Settings(**values)


# ============================================================
# 1. LLM 客户端
# ============================================================


class LLMStreamTests(unittest.TestCase):
    def test_streams_content_then_usage(self) -> None:
        response = FakeResponse(
            [
                json.dumps({"message": {"content": "你"}}).encode(),
                json.dumps(
                    {"message": {"content": "好"}, "model": "qwen2.5:7b"}
                ).encode(),
                json.dumps(
                    {
                        "done": True,
                        "model": "qwen2.5:7b",
                        "eval_count": 2,
                        "total_duration": 1_000_000,
                    }
                ).encode(),
            ]
        )
        session = FakeSession(response)

        chunks = list(
            llm.stream_chat(
                [{"role": "user", "content": "问题"}],
                settings=local_settings(),
                session=session,
            )
        )

        self.assertEqual([item.content for item in chunks], ["你", "好", ""])
        self.assertTrue(chunks[-1].done)
        self.assertEqual(chunks[-1].usage["eval_count"], 2)
        self.assertEqual(chunks[-1].usage["total_duration_ms"], 1.0)
        self.assertTrue(response.closed)
        payload = session.calls[0][1]["json"]
        self.assertTrue(payload["stream"])
        self.assertTrue(session.calls[0][1]["stream"])

    def test_cancel_stops_and_closes(self) -> None:
        state = {"cancel": False}

        def cancel() -> bool:
            return state["cancel"]

        response = FakeResponse(
            [
                json.dumps({"message": {"content": "你"}}).encode(),
                json.dumps({"message": {"content": "好"}}).encode(),
            ]
        )
        chunks = llm.stream_chat(
            [{"role": "user", "content": "问题"}],
            settings=local_settings(),
            session=FakeSession(response),
            cancel=cancel,
        )
        self.assertEqual(next(chunks).content, "你")
        state["cancel"] = True
        with self.assertRaises(llm.LLMError) as caught:
            next(chunks)
        self.assertEqual(caught.exception.kind, "llm_cancelled")
        self.assertTrue(response.closed)

    def test_http_error_has_no_body(self) -> None:
        response = FakeResponse([b"secret body"], status_code=500)
        with self.assertRaises(llm.LLMError) as caught:
            list(
                llm.stream_chat(
                    [{"role": "user", "content": "问题"}],
                    settings=local_settings(),
                    session=FakeSession(response),
                )
            )
        self.assertEqual(caught.exception.kind, "llm_http_error")
        self.assertNotIn("secret body", caught.exception.message)
        self.assertTrue(response.closed)

    def test_empty_stream_is_bad_response(self) -> None:
        response = FakeResponse([json.dumps({"done": True}).encode()])
        with self.assertRaises(llm.LLMError) as caught:
            list(
                llm.stream_chat(
                    [{"role": "user", "content": "问题"}],
                    settings=local_settings(),
                    session=FakeSession(response),
                )
            )
        self.assertEqual(caught.exception.kind, "llm_bad_response")

    def test_non_json_line_is_bad_response(self) -> None:
        response = FakeResponse([b"not-json"])
        with self.assertRaises(llm.LLMError) as caught:
            list(
                llm.stream_chat(
                    [{"role": "user", "content": "问题"}],
                    settings=local_settings(),
                    session=FakeSession(response),
                )
            )
        self.assertEqual(caught.exception.kind, "llm_bad_response")


# ============================================================
# 2. 本机链路
# ============================================================


class ChainStreamTests(unittest.TestCase):
    def test_emits_context_chunks_and_result(self) -> None:
        events = list(
            local_chain.iter_stream(
                "问题",
                retrieve=lambda _: query_data(),
                chat_stream=lambda messages, *, model=None, cancel=None: stream_chunks(
                    "结论 ", "[1]。"
                ),
            )
        )

        self.assertEqual(
            [item["type"] for item in events], ["context", "chunk", "chunk", "_result"]
        )
        final = events[-1]["answer"]
        self.assertEqual(final.answer, "结论 [1]。")
        self.assertEqual(final.cited_labels, ["[1]"])
        self.assertEqual(final.unknown_labels, [])
        self.assertGreaterEqual(final.timings_ms["first_token_ms"], 0.0)
        self.assertEqual(final.usage["eval_count"], 2)

    def test_empty_stream_raises(self) -> None:
        def only_done(messages, *, model=None, cancel=None):
            yield llm.LLMStreamChunk(content="", done=True)

        with self.assertRaises(local_chain.LocalChainError) as caught:
            list(
                local_chain.iter_stream(
                    "问题",
                    retrieve=lambda _: query_data(),
                    chat_stream=only_done,
                )
            )
        self.assertEqual(caught.exception.kind, "empty_answer")


# ============================================================
# 3. 生产门面
# ============================================================


class GenerationStreamTests(unittest.TestCase):
    def _run(self, final, chunks=("结论 [1]。",)):
        def fake_chain(question, *, chat_stream=None, cancelled=None, budget=None):
            yield {"type": "context", "context": built_context()}
            for text in chunks:
                yield {"type": "chunk", "text": text}
            yield {"type": "_result", "answer": final}

        with mock.patch.object(
            local_generation,
            "get_settings",
            return_value=Settings(generation_mode="local"),
        ), mock.patch.object(local_chain, "iter_stream", fake_chain):
            return list(local_generation.iter_stream("问题"))

    def test_references_then_chunks_then_answer(self) -> None:
        events = self._run(local_answer_result())

        self.assertEqual(
            [item["type"] for item in events], ["references", "chunk", "_answer"]
        )
        labels = {item.get("label", "") for item in events[0]["items"]}
        self.assertEqual(labels, {"", "[img-1]"})
        answer = events[-1]["answer"]
        self.assertEqual(answer.response, "结论 [1]。")
        self.assertEqual(answer.response_time, 0.124)
        self.assertEqual(len(answer.references), 2)

    def test_unverified_after_chunks_marks_streamed(self) -> None:
        final = local_answer_result(answer="一段没有引用的总结。", cited=())
        with self.assertRaises(local_generation.LocalGenerationError) as caught:
            self._run(final)
        self.assertEqual(caught.exception.kind, "local_unverified")
        self.assertTrue(caught.exception.streamed)

    def test_unverified_before_first_chunk_is_not_streamed(self) -> None:
        final = local_answer_result(answer="一段没有引用的总结。", cited=())
        with self.assertRaises(local_generation.LocalGenerationError) as caught:
            self._run(final, chunks=())
        self.assertEqual(caught.exception.kind, "local_unverified")
        self.assertFalse(caught.exception.streamed)

    def test_upstream_mode_raises_local_off(self) -> None:
        with mock.patch.object(
            local_generation,
            "get_settings",
            return_value=Settings(generation_mode="upstream"),
        ):
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                next(local_generation.iter_stream("问题"))
        self.assertEqual(caught.exception.kind, "local_off")


# ============================================================
# 4. 本机工具
# ============================================================


class LocalAnswerToolStreamTests(unittest.TestCase):
    def test_forwards_events_and_returns_payload(self) -> None:
        events = []

        def fake_stream(question, *, cancelled=None):
            yield {"type": "references", "items": [{"reference_id": "1"}]}
            yield {"type": "chunk", "text": "结论 "}
            yield {"type": "chunk", "text": "[1]"}
            yield {
                "type": "_answer",
                "answer": local_generation.LocalChatAnswer(
                    response="结论 [1]",
                    references=({"reference_id": "1", "file_path": "a.md"},),
                    response_time=1.2,
                    context=built_context(),
                    citations=("[1]",),
                    timings_ms={"first_token_ms": 20.0, "total_ms": 1200.0},
                ),
            }

        with mock.patch.object(local_generation, "iter_stream", fake_stream):
            result = local_answer.TOOL.invoke(
                ToolContext(emit=events.append, cancelled=lambda: False), query="知识库问题"
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["response"], "结论 [1]")
        self.assertEqual([item["type"] for item in events], ["references", "chunk", "chunk"])
        self.assertTrue(result.usage["streamed"])
        self.assertEqual(result.usage["first_token_ms"], 20.0)

    def test_failure_after_chunk_uses_streamed_code(self) -> None:
        events = []

        def fake_stream(question, *, cancelled=None):
            yield {"type": "chunk", "text": "半截"}
            raise local_generation.LocalGenerationError(
                "未通过引用校验", kind="local_unverified", streamed=True
            )

        with mock.patch.object(local_generation, "iter_stream", fake_stream):
            result = local_answer.TOOL.invoke(
                ToolContext(emit=events.append), query="知识库问题"
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "local_unverified_streamed")
        self.assertTrue(result.usage["streamed"])

    def test_failure_before_chunk_keeps_plain_code(self) -> None:
        def fake_stream(question, *, cancelled=None):
            raise local_generation.LocalGenerationError(
                "未通过引用校验", kind="local_unverified"
            )
            yield  # pragma: no cover

        with mock.patch.object(local_generation, "iter_stream", fake_stream):
            result = local_answer.TOOL.invoke(
                ToolContext(emit=lambda event: None), query="知识库问题"
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "local_unverified")
        self.assertFalse(result.usage.get("streamed", False))


# ============================================================
# 5. 技能运行时（emit 管线）
# ============================================================


class SkillRuntimeStreamTests(unittest.TestCase):
    def test_stream_answer_through_real_manager(self) -> None:
        def fake_stream(question, *, cancelled=None):
            self.assertIsNotNone(cancelled)
            self.assertFalse(cancelled())
            yield {"type": "references", "items": [{"reference_id": "1"}]}
            yield {"type": "chunk", "text": "结论 "}
            yield {"type": "chunk", "text": "[1]"}
            yield {
                "type": "_answer",
                "answer": local_generation.LocalChatAnswer(
                    response="结论 [1]",
                    references=({"reference_id": "1", "file_path": "a.md"},),
                    response_time=1.2,
                    context=built_context(),
                    citations=("[1]",),
                ),
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            production,
            "get_settings",
            return_value=SimpleNamespace(generation_mode="local"),
        ), mock.patch.object(local_generation, "iter_stream", fake_stream):
            runtime = production.build_runtime(audit=AuditLogger(Path(tmp)))
            try:
                events = list(runtime.stream_answer("知识库里有哪些项目？"))
            finally:
                runtime.close()

        self.assertEqual(
            [item["type"] for item in events],
            ["references", "chunk", "chunk", "_answer"],
        )
        self.assertEqual(events[-1]["answer"].response, "结论 [1]")

    def test_image_answer_without_chunks_is_usable(self) -> None:
        answer = SimpleNamespace(
            response="找到一张图片 [img-1]",
            references=({"kind": "image"},),
            response_time=0.5,
        )
        runtime = production.SkillRuntime(manager=mock.Mock())
        runtime.manager.run.return_value = production.SkillOutput(
            status="ok",
            answer="找到一张图片 [img-1]",
            evidence=[mock.Mock()],
            trace={"skill": "image_lookup", "elapsed_ms": 500.0},
        )
        # 图片技能没有 emit；manager 返回整段答案。
        with mock.patch.object(
            production, "references_from_output", return_value=[{"kind": "image"}]
        ), mock.patch.object(
            production, "_elapsed_seconds", return_value=0.5
        ):
            events = list(runtime.stream_answer("找一下图片"))

        self.assertEqual(events[-1]["type"], "_answer")
        self.assertIsNotNone(events[-1]["answer"])


# ============================================================
# 6. 路由
# ============================================================


class RouterStreamTests(unittest.TestCase):
    def test_event_order_and_done(self) -> None:
        answer = SimpleNamespace(response="结论 [1]", response_time=1.5)

        def fake_stream(question, *, cancelled=None):
            yield {"type": "references", "items": [{"reference_id": "1"}]}
            yield {"type": "chunk", "text": "结论 "}
            yield {"type": "chunk", "text": "[1]"}
            yield {"type": "_answer", "answer": answer}

        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=False
        ), mock.patch.object(
            local_generation, "iter_stream", fake_stream
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(
            [item["type"] for item in events],
            ["references", "chunk", "chunk", "done", "end"],
        )
        self.assertEqual(events[-2]["response_time"], 1.5)

    def test_failure_before_chunk_falls_back_upstream(self) -> None:
        def fake_stream(question, *, cancelled=None):
            raise local_generation.LocalGenerationError("失败", kind="local_off")
            yield  # pragma: no cover

        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=False
        ), mock.patch.object(
            local_generation, "iter_stream", fake_stream
        ), mock.patch.object(
            chat_router.lightrag,
            "query_stream",
            return_value=[{"response": "上游"}],
        ) as upstream:
            events = list(chat_router._stream_events("问题"))

        upstream.assert_called_once_with("问题")
        self.assertEqual(events[0], {"type": "chunk", "text": "上游"})
        self.assertEqual(events[-1], {"type": "end"})

    def test_failure_after_chunk_does_not_replay_upstream(self) -> None:
        def fake_stream(question, *, cancelled=None):
            yield {"type": "chunk", "text": "半截"}
            raise local_generation.LocalGenerationError(
                "失败", kind="local_unverified", streamed=True
            )

        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=False
        ), mock.patch.object(
            local_generation, "iter_stream", fake_stream
        ), mock.patch.object(
            chat_router.lightrag,
            "query_stream",
            return_value=[{"response": "上游"}],
        ) as upstream:
            events = list(chat_router._stream_events("问题"))

        upstream.assert_not_called()
        self.assertEqual(
            [item["type"] for item in events], ["chunk", "error", "end"]
        )
        self.assertIn("未回退上游", events[1]["message"])

    def test_skill_stream_forwards_events(self) -> None:
        runtime = mock.Mock()
        runtime.stream_answer.return_value = iter(
            [
                {"type": "references", "items": [{"reference_id": "1"}]},
                {"type": "chunk", "text": "结论 "},
                {"type": "chunk", "text": "[1]"},
                {"type": "_answer", "answer": SimpleNamespace(
                    response="结论 [1]", response_time=1.0
                ), "error": ""},
            ]
        )
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(
            [item["type"] for item in events],
            ["references", "chunk", "chunk", "done", "end"],
        )

    def test_skill_answer_without_chunks_emits_references_then_chunk(self) -> None:
        runtime = mock.Mock()
        runtime.stream_answer.return_value = iter(
            [
                {
                    "type": "_answer",
                    "answer": SimpleNamespace(
                        response="找到一张图片 [img-1]",
                        references=({"kind": "image", "label": "[img-1]"},),
                        response_time=0.5,
                    ),
                    "error": "",
                },
            ]
        )
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ):
            events = list(chat_router._stream_events("找一下图片"))

        self.assertEqual(
            [item["type"] for item in events],
            ["references", "chunk", "done", "end"],
        )
        self.assertEqual(events[0]["items"][0]["label"], "[img-1]")

    def test_skill_failure_before_chunk_falls_back(self) -> None:
        runtime = mock.Mock()
        runtime.stream_answer.return_value = iter(
            [{"type": "_answer", "answer": None, "error": "失败"}]
        )
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ), mock.patch.object(
            chat_router.lightrag,
            "query_stream",
            return_value=[{"response": "上游"}],
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(events[0], {"type": "chunk", "text": "上游"})

    def test_skill_failure_after_chunk_reports_error(self) -> None:
        runtime = mock.Mock()
        runtime.stream_answer.return_value = iter(
            [
                {"type": "chunk", "text": "半截"},
                {"type": "_answer", "answer": None, "error": "未通过校验"},
            ]
        )
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ), mock.patch.object(
            chat_router.lightrag, "query_stream"
        ) as upstream:
            events = list(chat_router._stream_events("问题"))

        upstream.assert_not_called()
        self.assertEqual(
            [item["type"] for item in events], ["chunk", "error", "end"]
        )
        self.assertEqual(events[1]["message"], "未通过校验")


if __name__ == "__main__":
    unittest.main()
