"""M1：/chat 与 /chat/stream 的持久化契约（不联网、不启动 HTTP 服务）。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from fastapi import Response

from app.memory import chat_bridge, runtime
from app.models.schemas import Question
from app.routers import chat as chat_router
from tests.memory_fixtures import memory_store


def _consume(response) -> str:
    async def collect() -> str:
        chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            chunks.append(chunk)
        return "".join(chunks)

    return asyncio.run(collect())


class ChatPersistenceTests(unittest.TestCase):
    def test_non_stream_persists_and_returns_header(self) -> None:
        with memory_store() as (store, _):
            with mock.patch.object(
                chat_router.lightrag,
                "query",
                return_value={
                    "response": "回答 8100",
                    "references": [{"reference_id": "1", "file_path": "a.md"}],
                },
            ):
                response = Response()
                result = chat_router.chat(
                    Question(message="端口是多少", client_message_id="c1"), response
                )

            self.assertEqual(result["response"], "回答 8100")
            session_id = response.headers.get("X-Session-Id")
            self.assertTrue(session_id)
            messages = store.list_messages(session_id)
            self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
            self.assertEqual(messages[1]["content"], "回答 8100")
            self.assertEqual(messages[1]["status"], "complete")
            self.assertEqual(messages[1]["references"][0]["file_path"], "a.md")

    def test_same_client_message_id_does_not_duplicate_user(self) -> None:
        with memory_store() as (store, _):
            with mock.patch.object(
                chat_router.lightrag, "query", return_value={"response": "回答"}
            ):
                first = Response()
                chat_router.chat(
                    Question(message="同一个问题", client_message_id="same"), first
                )
                session_id = first.headers.get("X-Session-Id")
                second = Response()
                chat_router.chat(
                    Question(
                        message="同一个问题",
                        session_id=session_id,
                        client_message_id="same",
                    ),
                    second,
                )
            messages = store.list_messages(session_id)
            self.assertEqual(sum(1 for item in messages if item["role"] == "user"), 1)
            self.assertGreaterEqual(
                sum(1 for item in messages if item["role"] == "assistant"), 2
            )

    def test_stream_persists_complete_content(self) -> None:
        with memory_store() as (store, _):
            def fake_events(
                message,
                *,
                force_upstream=False,
                force_bypass=False,
                **kwargs,
            ):
                yield {
                    "type": "references",
                    "items": [{"reference_id": "1", "file_path": "a.md"}],
                }
                yield {"type": "chunk", "text": "第一段"}
                yield {"type": "chunk", "text": "第二段"}
                yield {"type": "done", "response_time": 1.2, "verified": True}
                yield {"type": "end"}

            with mock.patch.object(chat_router, "_stream_events", fake_events):
                response = chat_router.chat_stream(
                    Question(message="流式问题", client_message_id="s1")
                )
                body = _consume(response)

            self.assertIn('"type": "chunk"', body)
            self.assertIn('"type": "end"', body)
            session_id = response.headers.get("X-Session-Id")
            messages = store.list_messages(session_id)
            assistant = [item for item in messages if item["role"] == "assistant"][0]
            self.assertEqual(assistant["content"], "第一段第二段")
            self.assertEqual(assistant["status"], "complete")
            self.assertEqual(assistant["references"][0]["file_path"], "a.md")
            self.assertEqual(assistant["meta"]["response_time"], 1.2)

    def test_stream_error_event_marks_error(self) -> None:
        with memory_store() as (store, _):
            def fake_events(
                message,
                *,
                force_upstream=False,
                force_bypass=False,
                **kwargs,
            ):
                yield {"type": "chunk", "text": "已输出"}
                yield {
                    "type": "error",
                    "message": "本机中断",
                    "retry_upstream": True,
                }
                yield {"type": "end"}

            with mock.patch.object(chat_router, "_stream_events", fake_events):
                response = chat_router.chat_stream(
                    Question(message="流式失败", client_message_id="e1")
                )
                _consume(response)

            session_id = response.headers.get("X-Session-Id")
            assistant = [
                item
                for item in store.list_messages(session_id)
                if item["role"] == "assistant"
            ][0]
            self.assertEqual(assistant["status"], "error")
            self.assertIn("⚠️ 本机中断", assistant["content"])
            self.assertTrue(assistant["meta"]["retry_upstream"])

    def test_stream_close_marks_partial(self) -> None:
        with memory_store() as (store, _):
            persistence = chat_bridge.begin_chat(
                Question(message="中途关闭", client_message_id="p1"), source="test"
            )
            self.assertIsNotNone(persistence)

            def events():
                yield {"type": "chunk", "text": "abc"}
                yield {"type": "chunk", "text": "def"}
                yield {"type": "end"}

            generator = chat_bridge.wrap_stream(events(), persistence)
            self.assertEqual(next(generator)["text"], "abc")
            self.assertEqual(next(generator)["text"], "def")
            generator.close()

            message = store.get_message(persistence.assistant_message_id)
            self.assertEqual(message["status"], "partial")
            self.assertEqual(message["content"], "abcdef")

    def test_exception_marks_error(self) -> None:
        with memory_store() as (store, _):
            with mock.patch.object(
                chat_router.lightrag, "query", side_effect=RuntimeError("boom")
            ):
                response = Response()
                with self.assertRaises(RuntimeError):
                    chat_router.chat(
                        Question(message="失败", client_message_id="f1"), response
                    )
            session_id = response.headers.get("X-Session-Id")
            assistant = [
                item
                for item in store.list_messages(session_id)
                if item["role"] == "assistant"
            ][0]
            self.assertEqual(assistant["status"], "error")
            self.assertEqual(assistant["meta"]["exception"], "RuntimeError")

    def test_memory_off_does_not_write_or_set_header(self) -> None:
        runtime.reset_for_tests(enabled=False)
        try:
            with mock.patch.object(
                chat_router.lightrag, "query", return_value={"response": "回答"}
            ):
                response = Response()
                chat_router.chat(Question(message="关闭状态"), response)
            self.assertIsNone(response.headers.get("X-Session-Id"))
            self.assertIsNone(runtime.get_store())
        finally:
            runtime.reset_for_tests()


if __name__ == "__main__":
    unittest.main()
