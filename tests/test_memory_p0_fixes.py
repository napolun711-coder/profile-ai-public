"""阶段 2A：记忆写入止血与生命周期修复（P0-1A / P0-1B）。

覆盖审计 E5–E10 的关键回归：
- 清空会话重置水位；
- 导入历史设置水位，不被二次提取；
- 已删除会话可重新导入；
- sensitive 不能被 confirm 成 active；
- 彻底删除清掉事件正文与来源 quote；
- FTS 重建回填。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.memory import fastmem
from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore
from tests.memory_fixtures import memory_store


def _payload(session_key: str = "c_import") -> dict:
    return {
        "sessions": [{
            "id": session_key,
            "title": "导入会话",
            "created_at": 1700000000000,
            "updated_at": 1700000005000,
            "messages": [
                {"role": "user", "text": "旧问题一", "ts": 1700000000000},
                {"role": "ai", "text": "旧回答一", "ts": 1700000001000},
                {"role": "user", "text": "旧问题二", "ts": 1700000002000},
            ],
        }]
    }


class LifecycleTests(unittest.TestCase):
    def test_clear_messages_resets_watermark(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "第一条")
            store.jobs.set_watermark(session["id"], 1)
            self.assertEqual(store.jobs.get_watermark(session["id"]), 1)

            store.clear_session_messages(session["id"])
            self.assertEqual(store.jobs.get_watermark(session["id"]), 0)

            store.add_user_message(session["id"], "清空后的新消息")
            self.assertEqual(store.jobs.count_user_messages_since_watermark(session["id"]), 1)

    def test_import_sets_watermark_to_last_seq(self) -> None:
        with memory_store() as (store, _):
            result = store.import_sessions(_payload())
            session_id = result["mapping"]["c_import"]
            self.assertEqual(store.jobs.get_watermark(session_id), 3)
            self.assertEqual(store.jobs.count_user_messages_since_watermark(session_id), 0)

    def test_deleted_session_can_be_reimported(self) -> None:
        with memory_store() as (store, _):
            first = store.import_sessions(_payload("c_deleted"))
            old_id = first["mapping"]["c_deleted"]
            store.delete_session(old_id, purge=False)

            second = store.import_sessions(_payload("c_deleted"))
            self.assertEqual(second["imported"], 1)
            new_id = second["mapping"]["c_deleted"]
            self.assertNotEqual(new_id, old_id)
            self.assertEqual(store.get_session(new_id)["status"], "active")
            self.assertEqual(len(store.list_messages(new_id)), 3)

    def test_sensitive_cannot_be_confirmed(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="身份证号=110101199001011234",
                subject="身份证号",
                kind="fact",
                source_type="user_stated",
                sensitivity="sensitive",
                status="pending",
            )
            with self.assertRaises(ValueError):
                store.memories.confirm(memory["id"])
            self.assertEqual(store.memories.get(memory["id"])["status"], "pending")

    def test_purge_removes_event_content_and_sources(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(session["id"], "我的项目代号是 A3")
            memory = store.memories.create(
                content="项目代号：A3",
                subject="项目代号",
                kind="fact",
                source_type="user_stated",
                status="active",
                source={
                    "message_id": message["id"],
                    "session_id": session["id"],
                    "quote": "我的项目代号是 A3",
                },
            )
            store.memories.delete(memory["id"], purge=True)
            with store.db.connect() as conn:
                events = conn.execute(
                    "SELECT before_json, after_json FROM memory_events WHERE memory_id = ?",
                    (memory["id"],),
                ).fetchall()
                sources = conn.execute(
                    "SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?",
                    (memory["id"],),
                ).fetchone()[0]
            self.assertTrue(events)
            self.assertTrue(all(row[0] is None and row[1] is None for row in events))
            self.assertEqual(sources, 0)


class FtsRebuildTests(unittest.TestCase):
    def test_recreated_fts_is_backfilled(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-ai-fts-") as tmpdir:
            path = Path(tmpdir) / "fts.db"
            store = MemoryStore(MemoryDatabase(path))
            store.initialize()
            session = store.create_session()
            store.add_user_message(session["id"], "这是一段用于重建索引的测试消息")

            # 模拟 FTS 表被删除/损坏：直接 drop，再用新实例初始化（会重建并回填）。
            conn = sqlite3.connect(str(path))
            conn.execute("DROP TABLE messages_fts")
            conn.commit()
            conn.close()

            rebuilt = MemoryStore(MemoryDatabase(path))
            rebuilt.initialize()
            with rebuilt.db.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
            self.assertGreaterEqual(int(count), 1)


if __name__ == "__main__":
    unittest.main()
