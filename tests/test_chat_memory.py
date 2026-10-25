"""M2：聊天链路注入（上游请求体 / 本机 messages / 跨会话 / 纠正 / 关闭开关）。"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from unittest import mock

from fastapi import Response

from app.clients import llm
from app.core.config import get_settings
from app.memory import context
from app.memory.chat_bridge import is_no_context_response
from app.memory.context import ChatContext
from app.models.schemas import Question
from app.routers import chat as chat_router
from app.services import local_chain, prompts
from app.services.validators.content import validate_content
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


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_context_enabled=True,
        memory_fast_memory=True,
        memory_upstream_history="on",
        memory_upstream_memory="on",
        memory_context_budget=900,
        memory_recent_history_messages=8,
        memory_recent_history_tokens=1200,
    )
    return replace(base, **overrides)


def _chat(session_id, message, client_id):
    response = Response()
    result = chat_router.chat(
        Question(
            message=message,
            session_id=session_id or "",
            client_message_id=client_id,
        ),
        response,
    )
    return result, response


class ChatMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        # 不让本机 .env 的 GENERATION_MODE / SKILL_ROUTING 影响本组测试。
        self._patches = [
            mock.patch.object(chat_router, "_generation_mode", return_value="upstream"),
            mock.patch.object(chat_router, "_skill_routing_on", return_value=False),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()

    def test_upstream_request_contains_history_and_memory(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            store.add_assistant_message(
                session["id"], content="之前的回答", status="complete"
            )
            memory = store.memories.create(
                content="以后回答都用中文",
                subject="回答语言",
                kind="preference",
                source_type="user_explicit",
                confidence=1.0,
            )
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session["id"], "项目代号是什么？", "c1")

            kwargs = fake.call_args.kwargs
            self.assertIn("conversation_history", kwargs)
            history = kwargs["conversation_history"]
            self.assertEqual([item["role"] for item in history], ["user", "assistant"])
            self.assertNotIn("项目代号是什么？", [item["content"] for item in history])
            self.assertIn("user_prompt", kwargs)
            self.assertIn("回答语言", kwargs["user_prompt"])
            self.assertIn("以后回答都用中文", kwargs["user_prompt"])
            self.assertEqual(memory["status"], "active")

    def test_new_session_uses_tier1_fact(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session_a = store.create_session()
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session_a["id"], "我的项目代号是 A3", "a1")
                active = store.memories.list(status="active")
                self.assertTrue(any("A3" in item["content"] for item in active))

                session_b = store.create_session()
                _chat(session_b["id"], "项目代号是什么？", "b1")

            prompt = fake.call_args.kwargs.get("user_prompt", "")
            self.assertIn("A3", prompt)

            # 会话 B 明确纠正；新会话 C 应该看到 B7。
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session_b["id"], "不是 A3，是 B7", "b2")
                session_c = store.create_session()
                _chat(session_c["id"], "项目代号是什么？", "c1")

            prompt_c = fake.call_args.kwargs.get("user_prompt", "")
            self.assertIn("B7", prompt_c)
            self.assertNotIn("A3", prompt_c)
            superseded = [
                item
                for item in store.memories.list(status="superseded")
                if "A3" in item["content"]
            ]
            self.assertTrue(superseded)

    def test_manual_memory_injected_and_delete_stops(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            memory = store.memories.create(
                content="以后回答都用中文",
                subject="回答语言",
                kind="preference",
                source_type="user_explicit",
                confidence=1.0,
            )
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                session = store.create_session()
                _chat(session["id"], "今天天气怎么样？", "m1")
                prompt = fake.call_args.kwargs.get("user_prompt", "")
                self.assertIn("回答语言", prompt)

                store.memories.delete(memory["id"])
                session2 = store.create_session()
                _chat(session2["id"], "今天天气怎么样？", "m2")
                prompt = fake.call_args.kwargs.get("user_prompt", "")
                self.assertNotIn("回答语言", prompt)
                self.assertNotIn("回答用中文", prompt)
                # 会话状态仍可存在；被删除的长期记忆不能出现。
                self.assertIn("会话状态", prompt)

    def test_context_off_keeps_old_request_body(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg(memory_context_enabled=False)
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            store.add_assistant_message(
                session["id"], content="之前的回答", status="complete"
            )
            store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session["id"], "现在的问题", "off1")
            self.assertEqual(fake.call_args.kwargs, {})

    def test_capability_probe_false_degrades_without_500(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=False), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session["id"], "现在的问题", "old1")
            self.assertEqual(fake.call_args.kwargs, {})

    def test_upstream_channels_are_independent(self) -> None:
        """阶段 0：历史 / 长期记忆分别外发；默认都不发。"""

        def run(cfg):
            with memory_store() as (store, _):
                session = store.create_session()
                store.add_user_message(session["id"], "之前的问题")
                store.add_assistant_message(
                    session["id"], content="之前的回答", status="complete"
                )
                store.memories.create(
                    content="以后回答都用中文",
                    subject="回答语言",
                    kind="preference",
                    source_type="user_explicit",
                    confidence=1.0,
                )
                fake = mock.Mock(return_value={"response": "好的"})
                with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                     mock.patch("app.memory.context.get_settings", return_value=cfg), \
                     mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                     mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                     mock.patch.object(chat_router.lightrag, "query", fake):
                    _chat(session["id"], "现在的问题", "x1")
                return fake.call_args.kwargs

        history_only = run(_cfg(memory_upstream_memory="off"))
        self.assertIn("conversation_history", history_only)
        # 会话状态随历史通道发送，但长期记忆不能跟着走。
        self.assertIn("会话状态", history_only.get("user_prompt", ""))
        self.assertNotIn("回答语言", history_only.get("user_prompt", ""))

        memory_only = run(
            _cfg(memory_upstream_history="off", memory_upstream_memory="on")
        )
        self.assertNotIn("conversation_history", memory_only)
        self.assertIn("回答语言", memory_only.get("user_prompt", ""))
        self.assertNotIn("会话状态", memory_only.get("user_prompt", ""))

        neither = run(
            _cfg(memory_upstream_history="off", memory_upstream_memory="off")
        )
        self.assertEqual(neither, {})

    def test_local_messages_order_and_prefix(self) -> None:
        captured = {}

        def fake_chat(messages, *, model=None):
            captured["messages"] = messages
            return llm.LLMReply(
                content="好的", model="fake", elapsed_ms=1.0, usage={}
            )

        result = local_chain.answer(
            "本轮问题",
            retrieve=lambda question: {"data": {}},
            chat=fake_chat,
            history=[
                {"role": "user", "content": "上一轮问题"},
                {"role": "assistant", "content": "上一轮回答"},
            ],
            memory_text="## 长期记忆\n- 回答语言：中文",
        )
        messages = captured["messages"]
        self.assertEqual(
            [item["role"] for item in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertNotIn("长期记忆", messages[0]["content"])
        self.assertIn("## 长期记忆", messages[-1]["content"])
        self.assertIn("本轮问题", messages[-1]["content"])
        self.assertEqual(result.answer, "好的")

    def test_prompts_default_unchanged_without_memory(self) -> None:
        messages = prompts.build_messages(question="问题", context_text="资料")
        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertIn("以下是与问题相关的资料", messages[-1]["content"])

    def test_memory_only_answer_allows_no_citation(self) -> None:
        memory_report = validate_content(
            answer="你的项目代号是 A3",
            citations={},
            memory_context=True,
        )
        self.assertTrue(memory_report.ok)
        self.assertTrue(any(issue.code == "no_citation" for issue in memory_report.warnings))
        plain_report = validate_content(
            answer="你的项目代号是 A3",
            citations={},
            memory_context=False,
        )
        self.assertFalse(plain_report.ok)

    def test_no_context_sentinels_are_detected_without_false_positives(self) -> None:
        self.assertTrue(
            is_no_context_response("No relevant context found for the query.")
        )
        self.assertTrue(
            is_no_context_response(
                "Sorry, I'm not able to provide an answer to that question.[no-context]"
            )
        )
        self.assertFalse(
            is_no_context_response("资料里没有找到相关内容，我会按现有资料回答。")
        )
        self.assertFalse(is_no_context_response("好的"))

    def test_no_context_answer_not_complete_and_not_in_history(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            fake = mock.Mock(
                return_value={
                    "response": "No relevant context found for the query."
                }
            )
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                _chat(session["id"], "查不到的问题", "nc1")

            assistant = [
                item for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertEqual(assistant["status"], "error")
            self.assertTrue(assistant["meta"]["no_context"])
            self.assertEqual(assistant["meta"]["error"], "no_context")

            current = store.add_user_message(session["id"], "下一轮")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=cfg,
            )
            self.assertIsNotNone(ctx)
            self.assertFalse(
                any(
                    "No relevant context" in item["content"]
                    for item in ctx.history
                )
            )

    def test_force_bypass_skips_retrieval(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            fake = mock.Mock(return_value={"response": "继续回答"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                response = Response()
                result = chat_router.chat(
                    Question(
                        message="不查资料继续",
                        session_id=session["id"],
                        client_message_id="bp1",
                        force_bypass=True,
                    ),
                    response,
                )
            self.assertEqual(result["response"], "继续回答")
            self.assertEqual(fake.call_args.kwargs.get("mode"), "bypass")
            # bypass 不读 user_prompt，会话状态必须并入 query。
            self.assertIn("会话状态", fake.call_args.args[0])
            self.assertIn("不查资料继续", fake.call_args.args[0])
            assistant = [
                item for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertTrue(assistant["meta"]["bypass"])

    def test_force_bypass_stream_uses_bypass_mode(self) -> None:
        calls = {}

        def fake_stream(message, **kwargs):
            calls.update(kwargs)
            yield {"response": "继续回答"}

        with mock.patch.object(
            chat_router.lightrag, "query_stream", fake_stream
        ):
            events = list(
                chat_router._stream_events("不查资料继续", force_bypass=True)
            )
        self.assertEqual(calls.get("mode"), "bypass")
        self.assertTrue(any(event.get("type") == "chunk" for event in events))

    def test_force_bypass_stream_sends_context_to_final_request(self) -> None:
        calls = {}
        context_value = ChatContext(
            history=[{"role": "user", "content": "上一轮要求 600 字"}],
            session_prompt="## 会话状态\n- 字数：600",
            user_prompt="## 会话状态\n- 字数：600",
        )

        def fake_stream(message, **kwargs):
            calls["message"] = message
            calls.update(kwargs)
            yield {"response": "继续回答"}

        with mock.patch.object(chat_router, "_generation_mode", return_value="upstream"), \
             mock.patch.object(chat_router, "_skill_routing_on", return_value=False), \
             mock.patch.object(chat_router, "get_settings", return_value=_cfg()), \
             mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
             mock.patch.object(chat_router.lightrag, "query_stream", fake_stream):
            events = list(
                chat_router._stream_events(
                    "不查资料继续",
                    force_bypass=True,
                    context=context_value,
                )
            )

        self.assertIn("600", calls.get("message", ""))
        self.assertEqual(calls.get("mode"), "bypass")
        self.assertEqual(
            calls.get("conversation_history"),
            [{"role": "user", "content": "上一轮要求 600 字"}],
        )
        self.assertIn("600", calls.get("user_prompt", ""))
        self.assertTrue(any(event.get("type") == "chunk" for event in events))
        self.assertEqual(
            context_value.trace.get("upstream_context_delivery"), "sent"
        )
        self.assertEqual(
            context_value.trace.get("upstream_context_fields"),
            ["conversation_history", "user_prompt"],
        )
        self.assertEqual(context_value.trace.get("upstream_history_count"), 1)

    def test_direct_local_stream_fallback_keeps_context(self) -> None:
        context_value = ChatContext(
            history=[{"role": "user", "content": "上一轮事实"}],
            session_prompt="## 会话状态\n- 只使用已提供事实",
            user_prompt="## 会话状态\n- 只使用已提供事实",
        )
        fallback = []

        def fake_upstream(message, context=None):
            fallback.append((message, context))
            yield {"type": "chunk", "text": "上游回答"}
            yield {"type": "end"}

        with mock.patch(
            "app.services.local_generation.iter_stream",
            side_effect=RuntimeError("local failed before first token"),
        ), mock.patch.object(chat_router, "_upstream_events", fake_upstream):
            events = list(
                chat_router._direct_local_stream_events(
                    "继续修改", context=context_value
                )
            )

        self.assertEqual(fallback[0][0], "继续修改")
        self.assertIs(fallback[0][1], context_value)
        self.assertTrue(any(event.get("text") == "上游回答" for event in events))

    def test_skill_stream_fallback_keeps_context(self) -> None:
        context_value = ChatContext(
            history=[{"role": "assistant", "content": "当前是方案 B"}],
            session_prompt="## 会话状态\n- 当前任务：方案 B",
            user_prompt="## 会话状态\n- 当前任务：方案 B",
        )
        fallback = []

        class FailingRuntime:
            def stream_answer(self, message, options=None):
                raise RuntimeError("skill failed before first token")

            def answer(self, message, options=None):
                return None

        def fake_upstream(message, context=None):
            fallback.append((message, context))
            yield {"type": "chunk", "text": "上游回答"}
            yield {"type": "end"}

        with mock.patch(
            "app.skills.production.get_runtime", return_value=FailingRuntime()
        ), mock.patch.object(chat_router, "_upstream_events", fake_upstream):
            events = list(
                chat_router._skill_stream_events(
                    "继续任务", context=context_value
                )
            )

        self.assertEqual(fallback[0][0], "继续任务")
        self.assertIs(fallback[0][1], context_value)
        self.assertTrue(any(event.get("text") == "上游回答" for event in events))

    def test_no_context_stream_emits_actionable_event(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()

            def fake_events(message, *, force_upstream=False, force_bypass=False):
                yield {
                    "type": "chunk",
                    "text": "No relevant context found for the query.",
                }
                yield {"type": "end"}

            with mock.patch.object(chat_router, "_stream_events", fake_events), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg):
                response = chat_router.chat_stream(
                    Question(
                        message="查不到的问题",
                        session_id=session["id"],
                        client_message_id="nc2",
                    )
                )
                body = _consume(response)

            self.assertIn('"type": "no_context"', body)
            assistant = [
                item for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertEqual(assistant["status"], "error")
            self.assertTrue(assistant["meta"]["no_context"])

    def test_persistence_unavailable_is_reported_as_failed(self) -> None:
        with memory_store() as (_store, _):
            cfg = _cfg()
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch(
                     "app.memory.chat_bridge.runtime.get_store", return_value=None
                 ), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                response = Response()
                result = chat_router.chat(
                    Question(message="会话库不可用", client_message_id="pu1"),
                    response,
                )
            self.assertEqual(result["response"], "好的")
            self.assertEqual(
                response.headers.get("X-Context-Status"), "failed"
            )
            self.assertEqual(
                response.headers.get("X-Context-Reason"),
                "persistence_unavailable",
            )

    def test_context_failure_is_explicit_but_non_blocking(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch(
                     "app.memory.chat_bridge.build_chat_context",
                     side_effect=RuntimeError("boom"),
                 ), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                response = Response()
                result = chat_router.chat(
                    Question(
                        message="上下文坏了也要回答",
                        session_id=session["id"],
                        client_message_id="cf1",
                    ),
                    response,
                )

            self.assertEqual(result["response"], "好的")
            self.assertEqual(
                response.headers.get("X-Context-Status"), "failed"
            )
            self.assertEqual(
                response.headers.get("X-Context-Reason"), "assembly_failed"
            )
            assistant = [
                item for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertEqual(assistant["status"], "complete")
            self.assertEqual(assistant["meta"]["context_status"], "failed")
            self.assertEqual(assistant["meta"]["context_reason"], "assembly_failed")

    def test_history_read_failure_is_explicit_but_non_blocking(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()
            first = store.add_user_message(session["id"], "上一轮事实")
            store.add_assistant_message(
                session["id"], content="上一轮回答", status="complete"
            )
            self.assertEqual(first["role"], "user")
            fake = mock.Mock(return_value={"response": "好的"})
            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch("app.memory.context.get_settings", return_value=cfg), \
                 mock.patch.object(
                     store, "list_messages", side_effect=RuntimeError("db read failed")
                 ), \
                 mock.patch.object(chat_router.lightrag, "supports_query_context", return_value=True), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg), \
                 mock.patch.object(chat_router.lightrag, "query", fake):
                response = Response()
                result = chat_router.chat(
                    Question(
                        message="历史读取失败也要回答",
                        session_id=session["id"],
                        client_message_id="hf1",
                    ),
                    response,
                )

            self.assertEqual(result["response"], "好的")
            self.assertEqual(response.headers.get("X-Context-Status"), "failed")
            self.assertEqual(
                response.headers.get("X-Context-Reason"), "assembly_failed"
            )
            self.assertEqual(fake.call_args.kwargs, {})
            # 异常路径不能留下“有历史”的虚假状态；聊天仍可回答但明确无上下文。
            assistant = [
                item
                for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertEqual(assistant["meta"]["context_status"], "failed")
            self.assertEqual(assistant["meta"]["context_reason"], "assembly_failed")

    def test_context_failure_stream_emits_status_event(self) -> None:
        with memory_store() as (store, _):
            cfg = _cfg()
            session = store.create_session()

            def fake_events(message, *, force_upstream=False, force_bypass=False):
                yield {"type": "chunk", "text": "回答"}
                yield {"type": "end"}

            with mock.patch("app.memory.chat_bridge.get_settings", return_value=cfg), \
                 mock.patch(
                     "app.memory.chat_bridge.build_chat_context",
                     side_effect=RuntimeError("boom"),
                 ), \
                 mock.patch.object(chat_router, "_stream_events", fake_events), \
                 mock.patch.object(chat_router, "get_settings", return_value=cfg):
                response = chat_router.chat_stream(
                    Question(
                        message="流式上下文坏了",
                        session_id=session["id"],
                        client_message_id="cs1",
                    )
                )
                body = _consume(response)

            self.assertIn('"type": "context"', body)
            self.assertIn('"status": "failed"', body)
            self.assertIn('"reason": "assembly_failed"', body)
            self.assertEqual(
                response.headers.get("X-Context-Status"), "failed"
            )
            assistant = [
                item for item in store.list_messages(session["id"])
                if item["role"] == "assistant"
            ][-1]
            self.assertEqual(assistant["meta"]["context_status"], "failed")


if __name__ == "__main__":
    unittest.main()
