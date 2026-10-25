"""M2 收口：审计事件、secret/sensitive 不变量、来源 quote、预算 trace。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.core.config import get_settings
from app.memory import context, fastmem
from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore
from tests.memory_fixtures import memory_store


class MemoryEventCompletenessTests(unittest.TestCase):
    def test_soft_delete_writes_event(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            store.memories.delete(memory["id"])
            actions = [event["action"] for event in store.memories.events(memory["id"])]
            self.assertIn("delete", actions)

    def test_purge_writes_delete_event(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="临时事实", subject="事实", kind="fact"
            )
            store.memories.delete(memory["id"], purge=True)
            actions = [event["action"] for event in store.memories.events(memory["id"])]
            self.assertIn("delete", actions)
            self.assertIsNone(store.memories.get(memory["id"], include_deleted=True))

    def test_same_content_merge_writes_event_and_bumps_version(self) -> None:
        with memory_store() as (store, _):
            first = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            second = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["version"], 2)
            actions = [event["action"] for event in store.memories.events(first["id"])]
            self.assertIn("merge", actions)

    def test_confirm_same_content_writes_events(self) -> None:
        with memory_store() as (store, _):
            active = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            candidate = store.memories.create(
                content="回答用中文",
                subject="回答语言",
                kind="preference",
                status="pending",
            )
            store.memories.confirm(candidate["id"])
            candidate_actions = [
                event["action"] for event in store.memories.events(candidate["id"])
            ]
            active_actions = [
                event["action"] for event in store.memories.events(active["id"])
            ]
            self.assertIn("merge", candidate_actions)
            self.assertIn("merge", active_actions)


class MemoryInvariantTests(unittest.TestCase):
    def test_update_rejects_secret(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="普通事实", subject="测试", kind="fact"
            )
            with self.assertRaises(ValueError):
                store.memories.update(memory["id"], sensitivity="secret")
            self.assertEqual(store.memories.get(memory["id"])["sensitivity"], "normal")

    def test_update_sensitive_forces_pending(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="普通事实", subject="测试", kind="fact"
            )
            updated = store.memories.update(memory["id"], sensitivity="sensitive")
            self.assertEqual(updated["sensitivity"], "sensitive")
            self.assertEqual(updated["status"], "pending")
            self.assertEqual(store.memories.stable_global(), [])

    def test_restore_model_inferred_stays_pending(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="模型推断的偏好",
                subject="偏好",
                kind="preference",
                source_type="model_inferred",
                status="pending",
            )
            store.memories.delete(memory["id"])
            restored = store.memories.restore(memory["id"])
            self.assertEqual(restored["status"], "pending")
            self.assertEqual(restored["source_type"], "model_inferred")
            self.assertIsNone(restored["deleted_at"])

    def test_restore_sensitive_stays_pending(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="我的身份证号是 110101199001011234",
                subject="证件",
                kind="fact",
                sensitivity="sensitive",
                status="active",
            )
            self.assertEqual(memory["status"], "pending")
            store.memories.delete(memory["id"])
            restored = store.memories.restore(memory["id"])
            self.assertEqual(restored["status"], "pending")
            self.assertEqual(restored["sensitivity"], "sensitive")

    def test_restore_normal_returns_active(self) -> None:
        with memory_store() as (store, _):
            memory = store.memories.create(
                content="回答用中文", subject="回答语言", kind="preference"
            )
            store.memories.delete(memory["id"])
            restored = store.memories.restore(memory["id"])
            self.assertEqual(restored["status"], "active")


class MemorySourceQuoteTests(unittest.TestCase):
    def test_fastmem_quote_is_exact_substring(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            raw = "我的项目代号是    A3"
            user = store.add_user_message(session["id"], raw)
            result = fastmem.apply(
                store.memories,
                raw,
                message_id=user["id"],
                session_id=session["id"],
            )
            self.assertTrue(result.created)
            source = store.memories.sources(result.created[0]["id"])[0]
            stored = store.get_message(user["id"])["content"]
            self.assertIn(source["quote"], stored)
            self.assertTrue(source["meta"]["quote_verified"])

    def test_unverified_quote_is_flagged(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            user = store.add_user_message(session["id"], "原文里没有这句话")
            memory = store.memories.create(
                content="另一句话",
                subject="测试",
                kind="fact",
                source={
                    "message_id": user["id"],
                    "session_id": session["id"],
                    "quote": "不存在的引用",
                },
            )
            source = store.memories.sources(memory["id"])[0]
            self.assertFalse(source["meta"]["quote_verified"])


class MemoryBudgetTraceTests(unittest.TestCase):
    def test_trace_contains_combined_budget(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.memories.create(
                content="以后回答都用中文",
                subject="回答语言",
                kind="preference",
                source_type="user_explicit",
                confidence=1.0,
            )
            current = store.add_user_message(session["id"], "项目代号是什么？")
            cfg = replace(
                get_settings(),
                memory_enabled=True,
                memory_context_enabled=True,
                memory_fast_memory=True,
                memory_upstream_history="on",
                memory_context_budget=900,
                memory_recent_history_messages=8,
                memory_recent_history_tokens=1200,
            )
            built = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=cfg,
            )
            self.assertIsNotNone(built)
            self.assertEqual(built.trace["combined_budget"], 2100)
            self.assertLessEqual(
                built.trace["injected_tokens"], built.trace["combined_budget"]
            )
            self.assertIn("budget_scope", built.trace)


class MemoryRestartPersistenceTests(unittest.TestCase):
    def test_reopen_same_database_keeps_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="profile-ai-restart-") as tmpdir:
            path = Path(tmpdir) / "restart.db"
            first = MemoryStore(MemoryDatabase(path))
            first.initialize()
            session = first.create_session()
            first.add_user_message(session["id"], "我的项目代号是 A3")
            first.add_assistant_message(
                session["id"], content="好的", status="complete"
            )
            memory = first.memories.create(
                content="项目代号是 A3", subject="项目代号", kind="fact"
            )

            # 短连接模式没有常驻连接：新建 store 等价于进程重启后重新打开数据库。
            second = MemoryStore(MemoryDatabase(path))
            second.initialize()
            self.assertIsNotNone(second.get_session(session["id"]))
            self.assertEqual(
                [item["content"] for item in second.list_messages(session["id"])],
                ["我的项目代号是 A3", "好的"],
            )
            self.assertEqual(second.memories.get(memory["id"])["content"], "项目代号是 A3")


if __name__ == "__main__":
    unittest.main()
