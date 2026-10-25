"""M2：上下文装配/预算/作用域。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory import context
from app.memory.util import utc_now
from tests.memory_fixtures import memory_store


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_context_enabled=True,
        memory_fast_memory=True,
        memory_upstream_history="on",
        memory_context_budget=900,
        memory_recent_history_messages=8,
        memory_recent_history_tokens=1200,
    )
    return replace(base, **overrides)


class MemoryContextTests(unittest.TestCase):
    def test_stable_global_and_recent_history(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            memory = store.memories.create(
                content="以后回答都用中文",
                subject="回答语言",
                kind="preference",
                source_type="user_explicit",
                confidence=1.0,
            )
            store.add_user_message(session["id"], "我之前的问题")
            store.add_assistant_message(
                session["id"], content="之前的回答", status="complete"
            )
            current = store.add_user_message(session["id"], "项目代号是什么？")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertEqual([item["role"] for item in ctx.history], ["user", "assistant"])
            self.assertNotIn("项目代号是什么？", [item["content"] for item in ctx.history])
            self.assertIn("回答语言", ctx.user_prompt)
            self.assertIn("以后回答都用中文", ctx.user_prompt)
            self.assertIn("记忆使用规则", ctx.user_prompt)
            self.assertEqual(ctx.trace["memory_ids"], [memory["id"]])
            self.assertEqual(ctx.trace["history_count"], 2)

    def test_context_disabled_returns_none(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            store.add_user_message(session["id"], "之前")
            current = store.add_user_message(session["id"], "现在")
            self.assertIsNone(
                context.build_chat_context(
                    store,
                    session["id"],
                    current["content"],
                    exclude_message_id=current["id"],
                    settings=_cfg(memory_context_enabled=False),
                )
            )
            self.assertIsNone(
                context.build_chat_context(
                    store,
                    session["id"],
                    current["content"],
                    exclude_message_id=current["id"],
                    settings=_cfg(memory_enabled=False),
                )
            )

    def test_empty_context_returns_none(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            current = store.add_user_message(session["id"], "只有本轮")
            self.assertIsNone(
                context.build_chat_context(
                    store,
                    session["id"],
                    current["content"],
                    exclude_message_id=current["id"],
                    settings=_cfg(),
                )
            )

    def test_topic_isolation(self) -> None:
        with memory_store() as (store, _):
            now = utc_now()
            with store.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO topics(id, name, created_at, updated_at) "
                    "VALUES ('t1', '项目A', ?, ?)",
                    (now, now),
                )
            global_session = store.create_session()
            topic_session = store.create_session(topic_id="t1")
            topic_memory = store.memories.create(
                content="项目 A 使用 SQLite",
                subject="技术栈",
                kind="fact",
                scope="topic",
                topic_id="t1",
                importance=5,
            )
            store.add_user_message(global_session["id"], "项目 A 用什么？")
            global_current = store.add_user_message(global_session["id"], "项目 A 用什么？")
            global_ctx = context.build_chat_context(
                store,
                global_session["id"],
                global_current["content"],
                exclude_message_id=global_current["id"],
                settings=_cfg(),
            )
            # 全局会话不应该拿到 topic 记忆；已有历史也无记忆。
            self.assertTrue(
                global_ctx is None
                or topic_memory["id"] not in global_ctx.trace["memory_ids"]
            )

            store.add_user_message(topic_session["id"], "项目 A 用什么？")
            topic_current = store.add_user_message(topic_session["id"], "项目 A 用什么？")
            topic_ctx = context.build_chat_context(
                store,
                topic_session["id"],
                topic_current["content"],
                exclude_message_id=topic_current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(topic_ctx)
            self.assertIn(topic_memory["id"], topic_ctx.trace["memory_ids"])

    def test_document_lookup_does_not_inject_background(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="引用依据时保留文件名、文号和日期",
                subject="依据引用",
                kind="background",
                importance=4,
            )
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "资料库里引用依据的原文")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertNotIn(memory["id"], ctx.trace["memory_ids"])

    def test_material_request_still_injects_relevant_memory(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="引用依据时保留文件名、文号和日期",
                subject="依据引用",
                kind="background",
                importance=4,
            )
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "写资料时引用依据怎么保留")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertIn(memory["id"], ctx.trace["memory_ids"])

    def test_natural_language_query_retrieves_memory(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="引用依据时保留文件名、文号和日期",
                subject="依据引用",
                kind="background",
                importance=4,
            )
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "引用依据要保留哪些信息")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertIn(memory["id"], ctx.trace["memory_ids"][:5])

    def test_stable_global_is_bounded(self) -> None:
        with memory_store() as (store, _):
            for index in range(6):
                store.memories.create(
                    content="偏好 %d：回答保持简洁" % index,
                    subject="偏好 %d" % index,
                    kind="preference",
                    importance=4,
                )
            relevant = store.memories.create(
                content="常用法规是《事项工作条例》",
                subject="常用法规",
                kind="background",
                importance=4,
            )
            session = store.create_session()
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "常用法规是什么")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            reasons = list(ctx.trace["memory_reasons"].values())
            self.assertLessEqual(reasons.count("stable_global"), 3)
            self.assertIn(relevant["id"], ctx.trace["memory_ids"][:5])

    def test_archived_topic_memory_not_injected(self) -> None:
        with memory_store() as (store, _):
            now = utc_now()
            with store.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO topics(id, name, status, created_at, updated_at) "
                    "VALUES ('t_archived', '已归档资料集', 'archived', ?, ?)",
                    (now, now),
                )
            session = store.create_session(topic_id="t_archived")
            memory = store.memories.create(
                content="已归档资料集仍使用旧流程",
                subject="办理流程",
                kind="fact",
                scope="topic",
                topic_id="t_archived",
                importance=5,
                pinned=True,
            )
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "现在的问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertNotIn(memory["id"], ctx.trace["memory_ids"])

    def test_expired_topic_memory_not_injected(self) -> None:
        with memory_store() as (store, _):
            now = utc_now()
            with store.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO topics(id, name, created_at, updated_at) "
                    "VALUES ('t_expired', '已过期事项', ?, ?)",
                    (now, now),
                )
            session = store.create_session(topic_id="t_expired")
            memory = store.memories.create(
                content="该事项曾使用旧版表格",
                subject="表格版本",
                kind="fact",
                scope="topic",
                topic_id="t_expired",
                importance=5,
                pinned=True,
                valid_to="2000-01-01T00:00:00Z",
            )
            store.add_user_message(session["id"], "之前的问题")
            current = store.add_user_message(session["id"], "现在的问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertNotIn(memory["id"], ctx.trace["memory_ids"])

    def test_history_token_budget(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for index in range(20):
                store.add_user_message(session["id"], "很长的历史消息" * 20 + str(index))
                store.add_assistant_message(
                    session["id"], content="很长的历史回答" * 20, status="complete"
                )
            current = store.add_user_message(session["id"], "当前问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(memory_recent_history_tokens=300),
            )
            self.assertIsNotNone(ctx)
            self.assertLessEqual(ctx.history_tokens, 300)
            self.assertLessEqual(len(ctx.history), 8)

    def test_history_long_assistant_truncated_not_dropped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "上一轮问题")
            store.add_assistant_message(
                session["id"], content="长" * 3000, status="complete"
            )
            current = store.add_user_message(session["id"], "当前问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(memory_recent_history_tokens=300),
            )
            self.assertIsNotNone(ctx)
            self.assertEqual([item["role"] for item in ctx.history], ["user", "assistant"])
            self.assertIn("已截断", ctx.history[1]["content"])
            self.assertLessEqual(ctx.trace["history_tokens"], 300)
            self.assertTrue(ctx.trace["history_truncated"])

    def test_history_long_user_message_truncated_not_dropped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "长" * 3000)
            store.add_assistant_message(
                session["id"], content="简短回答", status="complete"
            )
            current = store.add_user_message(session["id"], "当前问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(memory_recent_history_tokens=300),
            )
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.history[0]["role"], "user")
            self.assertIn("已截断", ctx.history[0]["content"])
            self.assertLessEqual(ctx.trace["history_tokens"], 300)
            self.assertTrue(ctx.trace["history_truncated"])

    def test_history_assistant_citations_stripped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "用户引用[1]不是历史回答")
            store.add_assistant_message(
                session["id"],
                content="结论[1]，依据[2]。",
                status="complete",
            )
            current = store.add_user_message(session["id"], "当前问题")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertIn("[1]", ctx.history[0]["content"])
            self.assertNotIn("[1]", ctx.history[1]["content"])
            self.assertNotIn("[2]", ctx.history[1]["content"])
            self.assertIn("原引用编号已移除", ctx.history[1]["content"])
            self.assertEqual(ctx.trace["history_citations_stripped"], 2)

    def test_history_starts_with_user(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_assistant_message(
                session["id"], content="孤立的助手消息", status="complete"
            )
            store.add_user_message(session["id"], "用户问题")
            store.add_assistant_message(
                session["id"], content="助手回答", status="complete"
            )
            current = store.add_user_message(session["id"], "当前")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx.history[0]["role"], "user")

    def test_history_read_failure_is_not_silently_empty(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "上一轮")
            current = store.add_user_message(session["id"], "当前")
            with mock.patch.object(
                store, "list_messages", side_effect=RuntimeError("db read failed")
            ):
                with self.assertRaises(RuntimeError):
                    context.build_chat_context(
                        store,
                        session["id"],
                        current["content"],
                        exclude_message_id=current["id"],
                        settings=_cfg(),
                    )


if __name__ == "__main__":
    unittest.main()
