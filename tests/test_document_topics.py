"""P0-3B：文档 ↔ 资料集映射（隔离记忆库）。"""

from __future__ import annotations

import unittest

from tests.memory_fixtures import memory_store


class DocumentTopicTests(unittest.TestCase):
    def test_schema_v5_and_roundtrip(self) -> None:
        with memory_store() as (store, _):
            self.assertEqual(store.db.status()["schema_version"], 5)
            topic = store.create_topic(name="资料集 A")
            item = store.set_document_topic(
                "资料/说明文档.MD",
                topic["id"],
                name="说明文档",
                file_id="f1",
            )
            self.assertEqual(item["topic_id"], topic["id"])
            self.assertEqual(item["file_path"], "资料/说明文档.MD")
            self.assertEqual(store.document_topic("说明文档.md")["topic_id"], topic["id"])
            self.assertEqual(store.document_topic_map()["说明文档.md"], topic["id"])
            self.assertTrue(store.clear_document_topic("说明文档.md"))
            self.assertIsNone(store.document_topic("说明文档.md"))

    def test_unknown_topic_is_rejected(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(ValueError):
                store.set_document_topic("a.md", "topic_missing")

    def test_twenty_paired_topics_do_not_leak(self) -> None:
        with memory_store() as (store, _):
            pairs = []
            for index in range(20):
                topic = store.create_topic(name="配对资料集 %02d" % (index + 1))
                file_path = "资料集 %02d.md" % (index + 1)
                store.set_document_topic(
                    file_path,
                    topic["id"],
                    name="资料集 %02d" % (index + 1),
                    file_id="f%02d" % (index + 1),
                )
                pairs.append((topic["id"], file_path))
            leaks = 0
            for topic_id, file_path in pairs:
                rows = store.documents_for_topic(topic_id)
                if [row["file_path"] for row in rows] != [file_path]:
                    leaks += 1
            self.assertEqual(leaks, 0)

    def test_clear_all_removes_document_topics(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="资料集 A")
            store.set_document_topic("a.md", topic["id"])
            counts = store.clear_all()
            self.assertEqual(counts.get("document_topics"), 1)
            self.assertEqual(store.document_topics(), [])


if __name__ == "__main__":
    unittest.main()
