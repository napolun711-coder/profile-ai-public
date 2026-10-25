"""P3F：本机已输出后失败时的“手动换上游重试”。

不启动 HTTP 服务、不联网：直接调用路由函数，守住三件事：
- 请求里的 ``force_upstream`` 只对本次请求生效，跳过本机与技能；
- 本机已输出后失败的 ``error`` 事件带 ``retry_upstream``，供前端显示手动入口；
- 上游错误不带该标记，避免把普通失败误标成“可换上游重试”。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from app.core.errors import UpstreamError
from app.models.schemas import Question
from app.routers import chat as chat_router
from app.services import local_generation


BASE_DIR = Path(__file__).resolve().parent.parent


class PageRetryContractTests(unittest.TestCase):
    """页面契约：手动入口必须真的接上，且不能变成自动重试。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")

    def test_request_can_force_upstream(self) -> None:
        self.assertIn("payload.force_upstream = true;", self.html)
        self.assertIn("body: JSON.stringify(payload)", self.html)

    def test_error_event_exposes_manual_entry(self) -> None:
        self.assertIn("event.retry_upstream === true", self.html)
        self.assertIn("view.__retryUpstream = true;", self.html)
        self.assertIn('button.textContent = "换上游重试"', self.html)
        self.assertIn("retryBtn.addEventListener(\"click\"", self.html)
        self.assertIn("retryWithUpstream(view, retryBtn)", self.html)

    def test_retry_is_not_automatic(self) -> None:
        # 错误分支只记录入口；真正的请求只由按钮点击触发。
        error_block = self.html.split('if (event.type === "error")', 1)[1].split(
            "continue;", 1
        )[0]
        self.assertNotIn("retryWithUpstream(", error_block)
        self.assertNotIn("streamAnswer(", error_block)

    def test_retry_answer_is_marked_and_persisted(self) -> None:
        self.assertIn("appendUpstreamRetryNote(view)", self.html)
        self.assertIn("上游重试 · 本次按你的操作跳过了本机模型", self.html)
        self.assertIn("upstream_retry = true", self.html)
        self.assertIn("retry_upstream = true", self.html)
        self.assertIn("view.__retryQuestion = lastUserText;", self.html)


class RequestSchemaTests(unittest.TestCase):
    def test_force_upstream_defaults_to_false(self) -> None:
        self.assertFalse(Question(message="问题").force_upstream)
        self.assertTrue(Question(message="问题", force_upstream=True).force_upstream)


class ChatForceUpstreamTests(unittest.TestCase):
    def test_chat_skips_local_and_skill(self) -> None:
        with mock.patch.object(chat_router, "_local_answer") as local, mock.patch.object(
            chat_router, "_skill_answer"
        ) as skill, mock.patch.object(
            chat_router.lightrag, "query", return_value={"response": "上游"}
        ) as query:
            result = chat_router.chat(Question(message="问题", force_upstream=True))

        self.assertEqual(result, {"response": "上游"})
        query.assert_called_once_with("问题")
        local.assert_not_called()
        skill.assert_not_called()


class StreamForceUpstreamTests(unittest.TestCase):
    def test_stream_bypasses_local_and_skill(self) -> None:
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch.object(
            local_generation, "iter_stream"
        ) as local, mock.patch.object(
            chat_router.lightrag,
            "query_stream",
            return_value=[{"response": "上游", "response_time": 0.5}],
        ) as upstream:
            events = list(chat_router._stream_events("问题", force_upstream=True))

        local.assert_not_called()
        upstream.assert_called_once_with("问题")
        self.assertEqual(
            [item["type"] for item in events], ["chunk", "done", "end"]
        )


class RetryFlagTests(unittest.TestCase):
    def test_local_failure_after_chunk_exposes_retry_flag(self) -> None:
        def fake_stream(question, *, cancelled=None):
            yield {"type": "chunk", "text": "半截回答"}
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
            chat_router.lightrag, "query_stream"
        ) as upstream:
            events = list(chat_router._stream_events("问题"))

        upstream.assert_not_called()
        error = next(item for item in events if item["type"] == "error")
        self.assertTrue(error["retry_upstream"])
        self.assertIn("未回退上游", error["message"])

    def test_local_failure_before_chunk_has_no_error_event(self) -> None:
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
        ):
            events = list(chat_router._stream_events("问题"))

        self.assertEqual(events[0], {"type": "chunk", "text": "上游"})
        self.assertFalse(any(item["type"] == "error" for item in events))

    def test_upstream_error_has_no_retry_flag(self) -> None:
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="upstream"
        ), mock.patch.object(
            chat_router, "_skill_answer", return_value=None
        ), mock.patch.object(
            chat_router.lightrag,
            "query_stream",
            side_effect=UpstreamError("上游失败"),
        ):
            events = list(chat_router._stream_events("问题"))

        error = next(item for item in events if item["type"] == "error")
        self.assertNotIn("retry_upstream", error)

    def test_skill_failure_after_chunk_exposes_retry_flag(self) -> None:
        runtime = mock.Mock()

        def broken_stream(question):
            yield {"type": "chunk", "text": "半截"}
            raise RuntimeError("boom")

        runtime.stream_answer.side_effect = broken_stream
        with mock.patch.object(
            chat_router, "_generation_mode", return_value="local"
        ), mock.patch.object(
            chat_router, "_skill_routing_on", return_value=True
        ), mock.patch(
            "app.skills.production.get_runtime", return_value=runtime
        ):
            events = list(chat_router._stream_events("问题"))

        error = next(item for item in events if item["type"] == "error")
        self.assertTrue(error["retry_upstream"])


if __name__ == "__main__":
    unittest.main()
