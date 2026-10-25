"""P2F：`/chat` 技能路由开关的离线单测。

不启动 HTTP 服务、不联网：直接调用路由函数，观察事件形状与回退行为。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.core.config import Settings, _build_settings
from app.models.schemas import Question
from app.plugins import Evidence, ToolResult
from app.plugins.adapters import knowledge as knowledge_adapter
from app.plugins.audit import AuditLogger
from app.routers import chat as chat_router
from app.skills import production


class FakeAnswer:
    response = "技能答案 [1]"
    references = ({"reference_id": "1", "file_path": "a.md"},)
    response_time = 1.25

    def to_payload(self):
        return {
            "response": self.response,
            "references": [dict(item) for item in self.references],
            "response_time": self.response_time,
        }


class ConfigTests(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertEqual(Settings().skill_routing, "off")

    def test_env_on(self) -> None:
        with mock.patch.dict(os.environ, {"SKILL_ROUTING": "on"}, clear=False):
            self.assertEqual(_build_settings().skill_routing, "on")

    def test_invalid_falls_back_to_off(self) -> None:
        with mock.patch.dict(os.environ, {"SKILL_ROUTING": "shadow"}, clear=False):
            self.assertEqual(_build_settings().skill_routing, "off")

    def test_case_insensitive(self) -> None:
        with mock.patch.dict(os.environ, {"SKILL_ROUTING": "ON"}, clear=False):
            self.assertEqual(_build_settings().skill_routing, "on")


class ChatRoutingTests(unittest.TestCase):
    def test_off_calls_upstream_once(self) -> None:
        with mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query", return_value={"response": "upstream"}
        ) as query:
            result = chat_router.chat(Question(message="问题"))

        self.assertEqual(result, {"response": "upstream"})
        query.assert_called_once_with("问题")

    def test_skill_unavailable_falls_back(self) -> None:
        with mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query", return_value={"response": "fallback"}
        ) as query:
            result = chat_router.chat(Question(message="问题"))

        self.assertEqual(result, {"response": "fallback"})
        query.assert_called_once_with("问题")


class StreamRoutingTests(unittest.TestCase):
    def test_skill_stream_events(self) -> None:
        answer = FakeAnswer()
        with mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch.object(
            chat_router, "_skill_answer", return_value=answer
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual([event["type"] for event in events], [
            "references",
            "chunk",
            "done",
            "end",
        ])
        self.assertEqual(events[0]["items"], [dict(item) for item in answer.references])
        self.assertEqual(events[1]["text"], answer.response)
        self.assertEqual(events[2]["response_time"], answer.response_time)

    def test_upstream_stream_mapping_unchanged(self) -> None:
        upstream = [
            {"references": [{"reference_id": "1"}]},
            {"response": "第一段"},
            {"response": "第二段", "response_time": 2.5},
            {"error": "上游错误"},
        ]
        with mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query_stream", return_value=upstream
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(
            events,
            [
                {"type": "references", "items": [{"reference_id": "1"}]},
                {"type": "chunk", "text": "第一段"},
                {"type": "chunk", "text": "第二段"},
                {"type": "done", "response_time": 2.5},
                {"type": "error", "message": "上游错误"},
                {"type": "end"},
            ],
        )

    def test_stream_skill_unavailable_falls_back(self) -> None:
        with mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag, "query_stream", return_value=[{"response": "up"}]
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(events[0], {"type": "chunk", "text": "up"})
        self.assertEqual(events[-1], {"type": "end"})


class SkillAnswerGuardTests(unittest.TestCase):
    def test_skill_answer_uses_runtime(self) -> None:
        runtime = SimpleNamespace(answer=lambda message: FakeAnswer())
        with mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ):
            answer = chat_router._skill_answer("问题")

        self.assertIsInstance(answer, FakeAnswer)

    def test_off_returns_none(self) -> None:
        with mock.patch.object(chat_router, "_skill_routing_on", return_value=False):
            self.assertIsNone(chat_router._skill_answer("问题"))

    def test_runtime_none_returns_none(self) -> None:
        with mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=None
        ):
            self.assertIsNone(chat_router._skill_answer("问题"))

    def test_runtime_exception_returns_none(self) -> None:
        runtime = SimpleNamespace(answer=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ):
            self.assertIsNone(chat_router._skill_answer("问题"))


class EndToEndOfflineTests(unittest.TestCase):
    def test_legacy_manager_direct_call_still_works(self) -> None:
        payload = {
            "response": "答案 [1]",
            "references": [{"reference_id": "1", "file_path": "a.md"}],
            "response_time": 1.0,
        }
        evidence = Evidence(
            evidence_id="reference:1",
            kind="reference",
            source_id="1",
            doc_name="a.md",
            metadata={"reference_id": "1"},
        )
        tool_result = ToolResult.success(payload, evidence=[evidence])

        with tempfile.TemporaryDirectory() as tmp:
            runtime = production.build_runtime(audit=AuditLogger(Path(tmp)))
            production._runtime = runtime
            production._build_failed = False
            try:
                with mock.patch.object(
                    chat_router, "_skill_routing_on", return_value=True
                ), mock.patch.object(
                    production, "skill_routing_enabled", return_value=True
                ), mock.patch.object(
                    knowledge_adapter.ANSWER_TOOL, "invoke", return_value=tool_result
                ):
                    answer = chat_router._skill_answer(
                        "知识库里有哪些项目"
                    )
            finally:
                production.reset_runtime()

        result = answer.to_payload()
        self.assertEqual(result["response"], "答案 [1]")
        self.assertEqual(result["references"][0]["file_path"], "a.md")
        self.assertEqual(result["references"][0]["reference_id"], "1")
        self.assertIn("response_time", result)


if __name__ == "__main__":
    unittest.main()
