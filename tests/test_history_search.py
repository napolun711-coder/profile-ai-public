"""M4-mini：只读历史检索（FTS5 / topic / 删除会话）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore


class HistorySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(MemoryDatabase(Path(self._tmp.name) / "history.db"))
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_search_returns_snippets_and_filters_deleted_sessions(self) -> None:
        session = self.store.create_session(title="项目背景资料集")
        self.store.add_user_message(session["id"], "我的项目背景从1979年开始，档案被销毁。")
        self.store.add_assistant_message(
            session["id"],
            content="资料里提到项目背景认定需要原始档案。",
            status="complete",
        )
        other = self.store.create_session(title="无关会话")
        self.store.add_user_message(other["id"], "今天天气不错。")

        results = self.store.search_history("项目背景", limit=10)
        self.assertTrue(results)
        self.assertTrue(all(item["session_id"] == session["id"] for item in results))
        self.assertTrue(any("项目背景" in item["snippet"] for item in results))

        self.store.delete_session(session["id"])
        self.assertEqual(self.store.search_history("项目背景", limit=10), [])

    def test_topic_filter(self) -> None:
        topic = self.store.create_topic(name="资料集A")
        bound = self.store.create_session(title="资料集A会话", topic_id=topic["id"])
        free = self.store.create_session(title="普通会话")
        self.store.add_user_message(bound["id"], "资料集A的项目背景资料。")
        self.store.add_user_message(free["id"], "资料集A之外也有项目背景。")

        results = self.store.search_history("项目背景", topic_id=topic["id"], limit=10)
        self.assertTrue(results)
        self.assertTrue(all(item["topic_id"] == topic["id"] for item in results))

    def test_short_query_returns_empty(self) -> None:
        self.assertEqual(self.store.search_history("工"), [])


if __name__ == "__main__":
    unittest.main()
