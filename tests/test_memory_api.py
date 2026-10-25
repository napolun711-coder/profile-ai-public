"""M2：/api/memory 路由函数（直接调用，不启动 HTTP 服务）。"""

from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.memory import runtime
from app.models.schemas import MemoryCreate, MemoryUpdate
from app.routers import memory as memory_api
from tests.memory_fixtures import memory_store


class MemoryApiTests(unittest.TestCase):
    def test_disabled_returns_false_and_503(self) -> None:
        runtime.reset_for_tests(enabled=False)
        try:
            listed = memory_api.list_memories(
                status=None,
                kind=None,
                scope=None,
                topic_id=None,
                q="",
                limit=50,
                offset=0,
                include_deleted=False,
            )
            self.assertFalse(listed["enabled"])
            self.assertEqual(listed["items"], [])
            with self.assertRaises(HTTPException) as caught:
                memory_api.create_memory(
                    MemoryCreate(content="事实", subject="主题", kind="fact")
                )
            self.assertEqual(caught.exception.status_code, 503)
        finally:
            runtime.reset_for_tests()

    def test_crud_and_lifecycle(self) -> None:
        with memory_store() as (store, _):
            created = memory_api.create_memory(
                MemoryCreate(
                    content="以后回答都用中文",
                    subject="回答语言",
                    kind="preference",
                    source_type="user_explicit",
                    confidence=1.0,
                )
            )["memory"]
            self.assertEqual(created["status"], "active")
            self.assertIn("回答语言", created["subject"])

            listed = memory_api.list_memories(
                status=None,
                kind=None,
                scope=None,
                topic_id=None,
                q="中文",
                limit=50,
                offset=0,
                include_deleted=False,
            )
            self.assertEqual([item["id"] for item in listed["items"]], [created["id"]])

            fetched = memory_api.get_memory(created["id"], include_deleted=False)["memory"]
            self.assertEqual(fetched["id"], created["id"])

            updated = memory_api.update_memory(
                created["id"], MemoryUpdate(content="以后回答都用中文，不要英文", importance=5)
            )["memory"]
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["importance"], 5)

            events = memory_api.memory_events(created["id"], limit=100)["items"]
            self.assertTrue(any(event["action"] == "update" for event in events))

            sources = memory_api.memory_sources(created["id"])["items"]
            self.assertIsInstance(sources, list)

            self.assertTrue(memory_api.delete_memory(created["id"], purge=False)["ok"])
            self.assertIsNone(
                store.memories.get(created["id"])
            )
            restored = memory_api.restore_memory(created["id"])["memory"]
            self.assertEqual(restored["status"], "active")

    def test_confirm_and_reject(self) -> None:
        with memory_store() as (store, _):
            pending = memory_api.create_memory(
                MemoryCreate(
                    content="用户可能喜欢简短回答",
                    subject="偏好",
                    kind="preference",
                    status="pending",
                    source_type="model_inferred",
                    confidence=0.5,
                )
            )["memory"]
            confirmed = memory_api.confirm_memory(pending["id"])["memory"]
            self.assertEqual(confirmed["status"], "active")

            pending2 = memory_api.create_memory(
                MemoryCreate(
                    content="另一条待确认",
                    subject="待确认",
                    kind="fact",
                    status="pending",
                    source_type="model_inferred",
                )
            )["memory"]
            self.assertTrue(memory_api.reject_memory(pending2["id"])["ok"])
            self.assertEqual(
                store.memories.get(pending2["id"], include_deleted=True)["status"],
                "deleted",
            )

    def test_topic_without_id_is_400(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(HTTPException) as caught:
                memory_api.create_memory(
                    MemoryCreate(
                        content="项目事实",
                        subject="事实",
                        kind="fact",
                        scope="topic",
                        topic_id="",
                    )
                )
            self.assertEqual(caught.exception.status_code, 400)

    def test_unknown_topic_is_400(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(HTTPException) as caught:
                memory_api.create_memory(
                    MemoryCreate(
                        content="项目事实",
                        subject="事实",
                        kind="fact",
                        scope="topic",
                        topic_id="topic_missing",
                    )
                )
            self.assertEqual(caught.exception.status_code, 400)

    def test_status_contains_context_flags(self) -> None:
        with memory_store() as (store, _):
            status = memory_api.memory_status()
            self.assertTrue(status["enabled"])
            self.assertIn("context_enabled", status)
            self.assertIn("fast_memory", status)
            self.assertIn("upstream_history", status)
            self.assertIn("worker_configured", status)
            self.assertIn("worker_running", status)
            self.assertTrue(status["recall_implemented"])
            self.assertIn("chat_topic_filter", status)
            self.assertIsInstance(status["worker"], bool)


if __name__ == "__main__":
    unittest.main()
