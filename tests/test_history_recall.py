"""M4：跨会话历史召回、topic 过滤、有界注入与 7 步闭环。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.config import get_settings
from app.memory import fastmem
from app.memory.context import build_chat_context
from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore


class HistoryRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(MemoryDatabase(Path(self._tmp.name) / "history.db"))
        self.store.initialize()
        self.cfg = replace(
            get_settings(),
            memory_enabled=True,
            memory_context_enabled=True,
            memory_recall_enabled=True,
            memory_fast_memory=True,
            memory_context_budget=900,
            memory_recent_history_tokens=1200,
            memory_recall_budget=400,
            memory_recall_max_items=5,
            memory_upstream_history="off",
            memory_upstream_memory="off",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cross_session_recall_includes_source(self) -> None:
        a = self.store.create_session(title="方案讨论")
        self.store.add_user_message(a["id"], "方案 X 的端口是 8100。")
        b = self.store.create_session(title="新会话")
        question = self.store.add_user_message(b["id"], "以前定的方案 X 端口是多少？")
        context = build_chat_context(
            self.store,
            b["id"],
            "以前定的方案 X 端口是多少？",
            exclude_message_id=question["id"],
            settings=self.cfg,
        )
        self.assertIsNotNone(context)
        self.assertIn("历史摘录", context.user_prompt)
        self.assertIn("8100", context.user_prompt)
        self.assertIn("方案讨论", context.user_prompt)
        self.assertLessEqual(context.trace["recall_tokens"], 400)

    def test_topic_filter_does_not_mix_projects(self) -> None:
        topic_a = self.store.create_topic(name="项目A")
        topic_b = self.store.create_topic(name="项目B")
        a = self.store.create_session(title="A会话", topic_id=topic_a["id"])
        b = self.store.create_session(title="B会话", topic_id=topic_b["id"])
        current = self.store.create_session(title="当前A", topic_id=topic_a["id"])
        self.store.add_user_message(a["id"], "项目A的部署端口是 8100。")
        self.store.add_user_message(b["id"], "项目B的部署端口是 9200。")
        question = self.store.add_user_message(current["id"], "部署端口是多少？")
        context = build_chat_context(
            self.store,
            current["id"],
            "部署端口是多少？",
            exclude_message_id=question["id"],
            settings=self.cfg,
        )
        self.assertIsNotNone(context)
        self.assertIn("8100", context.user_prompt)
        self.assertNotIn("9200", context.user_prompt)

    def test_deleted_session_is_not_recalled(self) -> None:
        a = self.store.create_session(title="待删会话")
        self.store.add_user_message(a["id"], "历史里的项目背景资料。")
        b = self.store.create_session(title="新会话")
        question = self.store.add_user_message(b["id"], "项目背景资料以前怎么说的？")
        self.store.delete_session(a["id"])
        context = build_chat_context(
            self.store,
            b["id"],
            "项目背景资料以前怎么说的？",
            exclude_message_id=question["id"],
            settings=self.cfg,
        )
        self.assertTrue(context is None or "历史摘录" not in context.user_prompt)

    def test_purge_suppresses_history_source(self) -> None:
        a = self.store.create_session(title="来源会话")
        message = self.store.add_user_message(a["id"], "我的项目代号是 A3。")
        memory = self.store.memories.create(
            content="项目代号是 A3",
            subject="项目代号",
            kind="fact",
            status="active",
            source_type="user_confirmed",
            source={
                "message_id": message["id"],
                "session_id": a["id"],
                "quote": "我的项目代号是 A3",
            },
        )
        self.assertTrue(self.store.search_history("A3"))
        self.store.memories.delete(memory["id"], purge=True)
        self.assertEqual(self.store.search_history("A3"), [])

    def test_seven_step_cross_session_loop(self) -> None:
        # 1. 会话 A 提供稳定信息（Tier 1 快记忆）。
        a = self.store.create_session(title="项目A")
        first = self.store.add_user_message(a["id"], "我的项目代号是 A3。")
        fastmem.apply(
            self.store.memories,
            "我的项目代号是 A3。",
            message_id=first["id"],
            session_id=a["id"],
        )
        active = self.store.memories.list(status="active", limit=10)
        self.assertTrue(any("A3" in item["content"] for item in active))

        # 2-4. 新会话 B 直接提问，上下文包含 A3 和来源会话。
        b = self.store.create_session(title="新会话")
        question = self.store.add_user_message(b["id"], "我的项目代号是什么？")
        context = build_chat_context(
            self.store,
            b["id"],
            "我的项目代号是什么？",
            exclude_message_id=question["id"],
            settings=self.cfg,
        )
        self.assertIsNotNone(context)
        self.assertIn("A3", context.user_prompt)
        self.assertIn("项目A", context.user_prompt)

        # 5. 会话 B 明确纠正。
        correction = self.store.add_user_message(b["id"], "不是 A3，是 B7。")
        fastmem.apply(
            self.store.memories,
            "不是 A3，是 B7。",
            message_id=correction["id"],
            session_id=b["id"],
        )
        contents = [item["content"] for item in self.store.memories.list(limit=20)]
        self.assertTrue(any("B7" in item for item in contents))
        self.assertTrue(
            any("A3" in item["content"] for item in self.store.memories.list(status="superseded", limit=20))
        )

        # 6. 再问时上下文使用 B7。
        context = build_chat_context(
            self.store,
            b["id"],
            "我的项目代号是什么？",
            exclude_message_id=correction["id"],
            settings=self.cfg,
        )
        self.assertIsNotNone(context)
        memory_block = context.user_prompt.split("## 历史对话摘录")[0]
        self.assertIn("B7", memory_block)
        self.assertNotIn("A3", memory_block)

        # 7. 彻底忘记后，历史召回与记忆都不再返回。
        for item in self.store.memories.list(status="active", limit=20):
            if "B7" in item["content"]:
                self.store.memories.delete(item["id"], purge=True)
        self.assertEqual(self.store.search_history("B7"), [])
        context = build_chat_context(
            self.store,
            b["id"],
            "我的项目代号是什么？",
            exclude_message_id=correction["id"],
            settings=self.cfg,
        )
        self.assertTrue(context is None or "B7" not in context.user_prompt)


if __name__ == "__main__":
    unittest.main()
