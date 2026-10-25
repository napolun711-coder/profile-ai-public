"""阶段 2B：资料集作用域（topics CRUD + 会话绑定 + 记忆隔离）。"""

from __future__ import annotations

import unittest

from app.memory import fastmem
from app.memory.retrieve import load_memories
from app.models.schemas import TopicCreate, TopicUpdate
from app.routers import topics as topics_api
from tests.memory_fixtures import memory_store


class TopicStoreTests(unittest.TestCase):
    def test_crud_and_duplicate_name(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="示例资料集", kind="topic")
            self.assertTrue(topic["id"].startswith("t_"))
            self.assertEqual(topic["status"], "active")
            self.assertEqual(store.get_topic(topic["id"])["name"], "示例资料集")
            self.assertEqual(len(store.list_topics()), 1)

            updated = store.update_topic(topic["id"], name="示例资料集（2026）")
            self.assertIn("2026", updated["name"])
            store.update_topic(topic["id"], status="archived")
            self.assertEqual(store.list_topics(status="archived")[0]["status"], "archived")

            with self.assertRaises(ValueError):
                store.create_topic(name="示例资料集（2026）")

    def test_session_binds_topic(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="事项 A")
            session = store.create_session(title="会话", topic_id=topic["id"])
            self.assertEqual(session["topic_id"], topic["id"])
            updated = store.update_session(session["id"], topic_id=topic["id"])
            self.assertEqual(updated["topic_id"], topic["id"])
            with self.assertRaises(ValueError):
                store.update_session(session["id"], topic_id="t_missing")

    def test_two_topics_do_not_conflict_on_same_subject(self) -> None:
        with memory_store() as (store, _):
            topic_a = store.create_topic(name="资料集 A")
            topic_b = store.create_topic(name="资料集 B")
            session_a = store.create_session(topic_id=topic_a["id"])
            session_b = store.create_session(topic_id=topic_b["id"])

            msg_a = store.add_user_message(session_a["id"], "我的项目代号是 A3")
            fastmem.apply(
                store.memories,
                "我的项目代号是 A3",
                message_id=msg_a["id"],
                session_id=session_a["id"],
                topic_id=topic_a["id"],
            )
            msg_b = store.add_user_message(session_b["id"], "我的项目代号是 B7")
            fastmem.apply(
                store.memories,
                "我的项目代号是 B7",
                message_id=msg_b["id"],
                session_id=session_b["id"],
                topic_id=topic_b["id"],
            )

            active_a = store.memories.list(status="active", scope="topic", topic_id=topic_a["id"])
            active_b = store.memories.list(status="active", scope="topic", topic_id=topic_b["id"])
            self.assertEqual(len(active_a), 1)
            self.assertEqual(len(active_b), 1)
            self.assertIn("A3", active_a[0]["content"])
            self.assertIn("B7", active_b[0]["content"])

            items_a = load_memories(store, "项目代号", topic_id=topic_a["id"])
            values_a = " ".join(item["memory"]["content"] for item in items_a)
            self.assertIn("A3", values_a)
            self.assertNotIn("B7", values_a)

    def test_topic_api_functions(self) -> None:
        with memory_store() as (store, _):
            created = topics_api.create_topic(TopicCreate(name="事项事项", kind="topic"))
            self.assertTrue(created["topic"]["id"].startswith("t_"))
            listed = topics_api.list_topics(status="active", limit=50)
            self.assertEqual(len(listed["topics"]), 1)
            updated = topics_api.update_topic(
                created["topic"]["id"], TopicUpdate(description="2026 年答复意见")
            )
            self.assertIn("2026", updated["topic"]["description"])


if __name__ == "__main__":
    unittest.main()
