"""M3-2：候选合并、冲突策略与来源校验。"""

from __future__ import annotations

import unittest

from app.memory.merge import apply_candidates
from tests.memory_fixtures import memory_store


def _candidate(
    store,
    session,
    text,
    *,
    subject="项目代号",
    content=None,
    confidence=0.9,
    status="active",
    source_type="user_stated",
    sensitivity="normal",
    message_id=None,
    quote=None,
):
    message = store.add_user_message(session["id"], text)
    return message, {
        "content": content if content is not None else text,
        "subject": subject,
        "kind": "fact",
        "scope": "global",
        "source_type": source_type,
        "sensitivity": sensitivity,
        "confidence": confidence,
        "importance": 3,
        "status": status,
        "message_id": message_id if message_id is not None else message["id"],
        "session_id": session["id"],
        "quote": quote if quote is not None else text,
        "extractor": "rule",
        "reason": "test",
    }


class MergeTests(unittest.TestCase):
    def test_active_candidate_creates_memory_with_verified_source(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(store, session, "我的项目代号是 A3")
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["written"], 1)
            self.assertEqual(result["active"], 1)
            memory = store.memories.list(status="active")[0]
            source = store.memories.sources(memory["id"])[0]
            self.assertTrue(source["meta"]["quote_verified"])

    def test_duplicate_same_content_merges_sources(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, first = _candidate(store, session, "我的项目代号是 A3")
            _, second = _candidate(store, session, "我的项目代号是 A3")
            apply_candidates(store, [first])
            result = apply_candidates(store, [second])
            self.assertEqual(result["written"], 1)
            active = store.memories.list(status="active")
            self.assertEqual(len(active), 1)
            self.assertEqual(len(store.memories.sources(active[0]["id"])), 2)

    def test_conflict_with_user_explicit_keeps_old_active(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            old = store.memories.create(
                content="项目代号是 A3",
                subject="项目代号",
                kind="fact",
                source_type="user_explicit",
                confidence=1.0,
            )
            _, candidate = _candidate(
                store, session, "我的项目代号是 B7", content="项目代号是 B7"
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["conflicted"], 1)
            self.assertEqual(store.memories.get(old["id"])["status"], "active")
            conflicts = store.memories.list(status="conflicted")
            self.assertEqual(len(conflicts), 1)
            self.assertIn("A3", store.memories.get(old["id"])["content"])

    def test_low_confidence_old_memory_becomes_conflicted(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            old = store.memories.create(
                content="项目代号是 A3",
                subject="项目代号",
                kind="fact",
                source_type="user_stated",
                confidence=0.5,
            )
            _, candidate = _candidate(
                store,
                session,
                "我的项目代号是 B7",
                content="项目代号是 B7",
                confidence=0.9,
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["mutated_existing"], 1)
            self.assertEqual(store.memories.get(old["id"])["status"], "conflicted")
            self.assertEqual(len(store.memories.list(status="conflicted")), 2)

    def test_user_edited_old_memory_is_not_overwritten(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            old = store.memories.create(
                content="项目代号是 A3",
                subject="项目代号",
                kind="fact",
                source_type="user_stated",
                confidence=0.5,
                user_edited=True,
            )
            _, candidate = _candidate(
                store,
                session,
                "我的项目代号是 B7",
                content="项目代号是 B7",
                confidence=0.95,
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["mutated_existing"], 0)
            self.assertEqual(store.memories.get(old["id"])["status"], "active")
            self.assertEqual(result["conflicted"], 1)

    def test_pending_candidate_is_not_active(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(
                store, session, "我的项目代号是 A3", status="pending"
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["pending"], 1)
            self.assertEqual(store.memories.list(status="active"), [])

    def test_force_pending_makes_candidate_pending(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(store, session, "我的项目代号是 A3")
            result = apply_candidates(store, [candidate], force_pending=True)
            self.assertEqual(result["pending"], 1)
            self.assertEqual(store.memories.list(status="active"), [])

    def test_quote_mismatch_is_skipped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(
                store, session, "我的项目代号是 A3", quote="不存在的引用"
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["unverified"], 1)
            self.assertEqual(store.memories.list(include_deleted=True), [])

    def test_missing_source_is_skipped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(store, session, "我的项目代号是 A3")
            candidate["message_id"] = ""
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["unverified"], 1)

    def test_secret_sensitivity_is_skipped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, candidate = _candidate(
                store, session, "我的项目代号是 A3", sensitivity="secret"
            )
            result = apply_candidates(store, [candidate])
            self.assertEqual(result["written"], 0)
            self.assertEqual(store.memories.list(include_deleted=True), [])

    def test_pending_same_content_is_not_duplicated(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, first = _candidate(
                store, session, "我的项目代号是 A3", status="pending"
            )
            _, second = _candidate(
                store, session, "我的项目代号是 A3", status="pending"
            )
            apply_candidates(store, [first])
            result = apply_candidates(store, [second])
            self.assertEqual(result["duplicate_pending"], 1)
            self.assertEqual(len(store.memories.list(status="pending")), 1)

    def test_active_candidate_promotes_pending_same_content(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            _, pending = _candidate(
                store, session, "我的项目代号是 A3", status="pending"
            )
            _, active = _candidate(store, session, "我的项目代号是 A3", status="active")
            apply_candidates(store, [pending])
            result = apply_candidates(store, [active])
            self.assertEqual(result["active"], 1)
            self.assertEqual(store.memories.list(status="pending"), [])
            self.assertEqual(len(store.memories.list(status="active")), 1)

    def test_topic_scope_is_preserved(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            with store.db.transaction() as conn:
                from app.memory.util import utc_now

                now = utc_now()
                conn.execute(
                    "INSERT INTO topics(id, name, created_at, updated_at) "
                    "VALUES ('t1', '项目A', ?, ?)",
                    (now, now),
                )
            _, candidate = _candidate(store, session, "我的项目代号是 A3")
            candidate["scope"] = "topic"
            candidate["topic_id"] = "t1"
            apply_candidates(store, [candidate])
            memory = store.memories.list(status="active")[0]
            self.assertEqual(memory["scope"], "topic")
            self.assertEqual(memory["topic_id"], "t1")


if __name__ == "__main__":
    unittest.main()
