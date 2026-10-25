"""M5：记忆导出/导入幂等、清空全部数据。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "memory.db"
        self.store = MemoryStore(MemoryDatabase(self.db_path))
        self.store.initialize()
        session = self.store.create_session(title="来源会话")
        message = self.store.add_user_message(session["id"], "我的项目代号是 A3。")
        self.memory = self.store.memories.create(
            content="我的项目代号是 A3",
            subject="项目代号",
            kind="fact",
            status="active",
            source_type="user_explicit",
            source={
                "message_id": message["id"],
                "session_id": session["id"],
                "quote": "我的项目代号是 A3",
            },
        )
        self.session_id = session["id"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_export_import_is_idempotent(self) -> None:
        payload = self.store.export_memories()
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["items"]), 1)
        self.assertTrue(payload["items"][0]["sources"])
        self.assertIn("topics", payload)
        self.assertIn("document_topics", payload)

        first = self.store.import_memories(payload)
        self.assertEqual(first["imported"], 0)
        self.assertEqual(first["skipped"], 1)

        other_store = MemoryStore(MemoryDatabase(self.root / "other.db"))
        other_store.initialize()
        second = other_store.import_memories(payload)
        self.assertEqual(second["imported"], 1)
        third = other_store.import_memories(payload)
        self.assertEqual(third["imported"], 0)
        self.assertEqual(third["skipped"], 1)

    def test_export_import_topics_and_document_tags(self) -> None:
        topic = self.store.create_topic(name="资料集 A")
        self.store.set_document_topic(
            "资料集A.md", topic["id"], name="说明文档", file_id="f1"
        )
        payload = self.store.export_memories()
        self.assertTrue(any(item["id"] == topic["id"] for item in payload["topics"]))
        self.assertEqual(payload["document_topics"][0]["document_key"], "资料集a.md")

        other = MemoryStore(MemoryDatabase(self.root / "other.db"))
        other.initialize()
        result = other.import_memories(payload)
        self.assertEqual(result["topics_imported"], 1)
        self.assertEqual(result["document_topics_imported"], 1)
        self.assertEqual(other.get_topic(topic["id"])["name"], "资料集 A")
        self.assertEqual(
            other.document_topic("资料集A.md")["topic_id"], topic["id"]
        )

        again = other.import_memories(payload)
        self.assertEqual(again["topics_imported"], 0)
        self.assertEqual(again["topics_skipped"], 1)
        self.assertEqual(again["document_topics_imported"], 0)
        self.assertEqual(again["document_topics_skipped"], 1)

    def test_import_topic_scoped_memory_after_topics(self) -> None:
        topic = self.store.create_topic(name="资料集 A")
        self.store.memories.create(
            content="本案使用模板 A",
            subject="模板",
            kind="preference",
            scope="topic",
            topic_id=topic["id"],
            status="active",
            source_type="user_explicit",
        )
        payload = self.store.export_memories()
        other = MemoryStore(MemoryDatabase(self.root / "other-topic.db"))
        other.initialize()
        result = other.import_memories(payload)
        self.assertEqual(result["topics_imported"], 1)
        self.assertGreaterEqual(result["imported"], 1)
        imported = other.memories.list(status="active", scope="topic", limit=10)
        self.assertTrue(any(item["topic_id"] == topic["id"] for item in imported))

    def test_clear_all_removes_data_and_writes_tombstone(self) -> None:
        counts = self.store.clear_all()
        self.assertGreaterEqual(counts["memories"], 1)
        self.assertEqual(self.store.list_sessions(limit=10), [])
        self.assertEqual(self.store.search_history("项目代号", limit=10), [])
        self.assertEqual(self.store.memories.list(include_deleted=True, limit=10), [])
        lines = self.store.tombstones.path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any(json.loads(line).get("kind") == "all" for line in lines))

    def test_clear_all_can_purge_backups(self) -> None:
        backups = self.root / "backups" / "backup-old"
        backups.mkdir(parents=True)
        (backups / "manifest.json").write_text("{}", encoding="utf-8")
        self.store.clear_all(purge_backups=True, backup_root=self.root / "backups")
        self.assertFalse((self.root / "backups" / "backup-old").exists())


if __name__ == "__main__":
    unittest.main()
