"""M2：长期记忆仓储（去重/冲突/确认/版本/来源/删除）。"""

from __future__ import annotations

import unittest

from tests.memory_fixtures import memory_store


class MemoryRepoTests(unittest.TestCase):
    def test_create_stable_and_search(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="以后回答都用中文",
                subject="回答语言",
                kind="preference",
                source_type="user_explicit",
                confidence=1.0,
            )
            self.assertEqual(memory["status"], "active")
            self.assertEqual(memory["scope_key"], "global")
            self.assertEqual(
                [item["id"] for item in store.memories.stable_global()], [memory["id"]]
            )
            hits = store.memories.search("中文", scope_keys=["global"])
            self.assertEqual([item["id"] for item in hits], [memory["id"]])

    def test_duplicate_same_content_merges_source(self) -> None:
        with memory_store() as (store, _):
            first = store.memories.create(
                content="用户偏好中文", subject="回答语言", kind="preference"
            )
            second = store.memories.create(
                content="用户偏好中文", subject="回答语言", kind="preference"
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(store.memories.list(status="active")), 1)

    def test_same_key_different_content_becomes_conflicted(self) -> None:
        with memory_store() as (store, _):
            first = store.memories.create(
                content="项目代号是 A3", subject="项目代号", kind="fact"
            )
            second = store.memories.create(
                content="项目代号是 B7", subject="项目代号", kind="fact"
            )
            self.assertEqual(first["status"], "active")
            self.assertEqual(second["status"], "conflicted")
            self.assertEqual(second["meta"]["conflict_with"], first["id"])

    def test_update_increments_version_and_event(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="项目代号是 A3", subject="项目代号", kind="fact"
            )
            updated = store.memories.update(
                memory["id"], content="项目代号是 A3，用于测试", importance=5
            )
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["importance"], 5)
            events = store.memories.events(memory["id"])
            self.assertEqual(events[0]["action"], "update")
            self.assertEqual(events[0]["before"]["content"], "项目代号是 A3")

    def test_confirm_same_content_merges_candidate(self) -> None:
        with memory_store() as (store, _):
            active = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            candidate = store.memories.create(
                content="回答用中文",
                subject="回答语言",
                kind="preference",
                status="pending",
                source_type="user_stated",
            )
            # create 同 canonical 但 pending 不触发 active 唯一键；确认时应合并而不是唯一键冲突。
            result = store.memories.confirm(candidate["id"])
            self.assertEqual(result["id"], active["id"])
            self.assertEqual(
                store.memories.get(candidate["id"], include_deleted=True)["status"],
                "deleted",
            )

    def test_confirm_different_content_supersedes_active(self) -> None:
        with memory_store() as (store, _):
            old = store.memories.create(
                content="项目代号是 A3", subject="项目代号", kind="fact"
            )
            candidate = store.memories.create(
                content="项目代号是 B7",
                subject="项目代号",
                kind="fact",
                status="conflicted",
            )
            new = store.memories.confirm(candidate["id"])
            self.assertEqual(new["status"], "active")
            self.assertEqual(store.memories.get(old["id"])["status"], "superseded")
            self.assertEqual(store.memories.get(old["id"])["superseded_by"], new["id"])

    def test_sources_and_events(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            user = store.add_user_message(session["id"], "我的项目代号是 A3")
            memory = store.memories.create(
                content="我的项目代号是 A3",
                subject="项目代号",
                kind="fact",
                source={
                    "message_id": user["id"],
                    "session_id": session["id"],
                    "quote": "我的项目代号是 A3",
                },
            )
            sources = store.memories.sources(memory["id"])
            self.assertEqual(sources[0]["message_id"], user["id"])
            self.assertEqual(sources[0]["quote"], "我的项目代号是 A3")
            self.assertEqual(store.memories.events(memory["id"])[0]["action"], "create")

    def test_delete_restore_and_fts(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            self.assertTrue(store.memories.delete(memory["id"]))
            self.assertIsNone(store.memories.get(memory["id"]))
            self.assertEqual(store.memories.search("中文", scope_keys=["global"]), [])
            restored = store.memories.restore(memory["id"])
            self.assertEqual(restored["status"], "active")
            self.assertEqual(
                [item["id"] for item in store.memories.search("中文", scope_keys=["global"])],
                [memory["id"]],
            )

    def test_reject_pending(self) -> None:
        with memory_store() as (store, _):
            pending = store.memories.create(
                content="可能是用户的偏好",
                subject="偏好",
                kind="preference",
                status="pending",
            )
            self.assertTrue(store.memories.reject(pending["id"]))
            self.assertEqual(
                store.memories.get(pending["id"], include_deleted=True)["status"],
                "deleted",
            )
            self.assertEqual(store.memories.search("偏好", scope_keys=["global"]), [])

    def test_model_inferred_cannot_be_active(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="模型推断的偏好",
                subject="偏好",
                kind="preference",
                source_type="model_inferred",
                status="active",
            )
            self.assertEqual(memory["status"], "pending")
            self.assertEqual(store.memories.stable_global(), [])

    def test_secret_never_stored_and_sensitive_not_active(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(ValueError):
                store.memories.create(
                    content="secret",
                    subject="凭据",
                    kind="fact",
                    sensitivity="secret",
                    status="active",
                )
            sensitive = store.memories.create(
                content="我的身份证号是 110101199001011234",
                subject="证件",
                kind="fact",
                sensitivity="sensitive",
                status="active",
            )
            self.assertEqual(sensitive["status"], "pending")
            self.assertEqual(store.memories.stable_global(), [])

    def test_topic_scope_requires_topic_id(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(ValueError):
                store.memories.create(
                    content="项目事实",
                    subject="事实",
                    kind="fact",
                    scope="topic",
                    topic_id="",
                )

    def test_purge_keeps_events(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="临时事实", subject="事实", kind="fact"
            )
            self.assertTrue(store.memories.delete(memory["id"], purge=True))
            self.assertIsNone(store.memories.get(memory["id"], include_deleted=True))
            events = store.memories.events(memory["id"])
            self.assertTrue(any(event["action"] == "create" for event in events))


if __name__ == "__main__":
    unittest.main()
