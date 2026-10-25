"""M1：SQLite 会话/消息/索引行为。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.memory.db import SCHEMA_VERSION
from tests.memory_fixtures import memory_store


class MemoryStoreTests(unittest.TestCase):
    def test_initialize_and_status(self) -> None:
        with memory_store() as (store, root):
            status = store.status()
            self.assertTrue(status["ready"])
            self.assertTrue(status["fts_available"])
            self.assertEqual(status["schema_version"], SCHEMA_VERSION)
            self.assertTrue((root / "test.db").is_file())

    def test_session_and_message_order(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            self.assertEqual(session["title"], "")
            user = store.add_user_message(
                session["id"], "我的项目代号是 A3", client_message_id="c1"
            )
            assistant = store.add_assistant_message(
                session["id"], request_id="c1", reply_to_id=user["id"]
            )
            store.update_message(
                assistant["id"], content="已记住 A3", status="complete", completed=True
            )

            messages = store.list_messages(session["id"])
            self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
            self.assertEqual([item["seq"] for item in messages], [1, 2])
            self.assertEqual(store.get_session(session["id"])["title"], "我的项目代号是 A3")

    def test_user_message_idempotent(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            first = store.add_user_message(
                session["id"], "重复消息", client_message_id="same"
            )
            second = store.add_user_message(
                session["id"], "重复消息", client_message_id="same"
            )
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(store.count_messages(session["id"]), 1)

    def test_fts_finds_message(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "方案 X 的端口是 8100")
            with store.db.connect() as conn:
                rows = conn.execute(
                    "SELECT content FROM messages_fts WHERE messages_fts MATCH ?",
                    ("8100",),
                ).fetchall()
            self.assertTrue(any("8100" in row["content"] for row in rows))

    def test_clear_messages_removes_fts(self) -> None:
        with memory_store() as (store, root):
            session = store.create_session()
            store.add_user_message(session["id"], "需要被清空的 8100")
            self.assertEqual(store.clear_session_messages(session["id"]), 1)
            self.assertEqual(store.list_messages(session["id"]), [])
            with store.db.connect() as conn:
                rows = conn.execute(
                    "SELECT content FROM messages_fts WHERE messages_fts MATCH ?",
                    ("8100",),
                ).fetchall()
            self.assertEqual(rows, [])
            log = (root / "deletions.jsonl").read_text(encoding="utf-8")
            self.assertIn("session_messages", log)

    def test_soft_delete_hides_session(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "会被软删除")
            self.assertTrue(store.delete_session(session["id"]))
            self.assertIsNone(store.get_session(session["id"]))
            self.assertEqual(store.list_sessions(), [])
            with self.assertRaises(Exception):
                store.list_messages(session["id"])
            # 软删除保留消息行（可审计），但必须同时清掉 FTS，避免后续检索命中。
            with store.db.connect() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (session["id"],),
                ).fetchone()[0]
                fts = conn.execute(
                    "SELECT content FROM messages_fts WHERE messages_fts MATCH ?",
                    ("软删除",),
                ).fetchall()
            self.assertEqual(count, 1)
            self.assertEqual(fts, [])
            status = store.status()
            self.assertEqual(status["session_count"], 0)
            self.assertEqual(status["message_count"], 0)
            self.assertEqual(status["deleted_message_count"], 1)

    def test_purge_delete_removes_rows_and_fts(self) -> None:
        with memory_store() as (store, root):
            session = store.create_session()
            store.add_user_message(session["id"], "彻底删除 8100")
            self.assertTrue(store.delete_session(session["id"], purge=True))
            self.assertIsNone(store.get_session(session["id"]))
            with store.db.connect() as conn:
                messages = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                    (session["id"],),
                ).fetchone()[0]
                fts = conn.execute(
                    "SELECT content FROM messages_fts WHERE messages_fts MATCH ?",
                    ("8100",),
                ).fetchall()
            self.assertEqual(messages, 0)
            self.assertEqual(fts, [])
            log = (root / "deletions.jsonl").read_text(encoding="utf-8")
            self.assertIn('"kind":"session"', log)
            self.assertIn('"mode":"purge"', log)

    def test_soft_delete_then_purge_keeps_fts_consistent(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "软删后彻底删 8100")
            self.assertTrue(store.delete_session(session["id"]))
            # FTS 已在软删除时移除；彻底删除不能因为重复 delete 而报 malformed。
            self.assertTrue(store.delete_session(session["id"], purge=True))
            with store.db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                rows = conn.execute(
                    "SELECT content FROM messages_fts WHERE messages_fts MATCH ?",
                    ("8100",),
                ).fetchall()
            self.assertEqual(rows, [])

    def test_repair_stale_streaming(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            assistant = store.add_assistant_message(
                session["id"], status="streaming", content="半截"
            )
            old = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            with store.db.connect() as conn:
                conn.execute(
                    "UPDATE messages SET updated_at = ? WHERE id = ?",
                    (old, assistant["id"]),
                )
                conn.commit()
            self.assertEqual(store.repair_stale_streaming(older_than_seconds=60), 1)
            self.assertEqual(store.get_message(assistant["id"])["status"], "partial")

    def test_redaction_on_persist(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(
                session["id"], "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz123456"
            )
            message = store.list_messages(session["id"])[0]
            self.assertIn("[已隐藏的凭据]", message["content"])
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", message["content"])
            self.assertTrue(message["meta"]["redaction"]["redacted"])

    def test_import_is_idempotent_and_maps_roles(self) -> None:
        with memory_store() as (store, _):
            payload = {
                "sessions": [
                    {
                        "id": "c_legacy_1",
                        "title": "旧会话",
                        "created_at": 1700000000000,
                        "updated_at": 1700000005000,
                        "messages": [
                            {"role": "user", "text": "旧问题", "ts": 1700000000000},
                            {
                                "role": "ai",
                                "text": "旧回答",
                                "ts": 1700000001000,
                                "references": [{"reference_id": "1", "file_path": "a.md"}],
                            },
                        ],
                    }
                ]
            }
            first = store.import_sessions(payload)
            second = store.import_sessions(payload)
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["skipped"], 1)
            mapped = first["mapping"]["c_legacy_1"]
            messages = store.list_messages(mapped)
            self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
            self.assertEqual(messages[1]["references"][0]["file_path"], "a.md")
            self.assertEqual(store.get_session(mapped)["client_session_id"], "c_legacy_1")

    def test_import_skips_empty_session(self) -> None:
        with memory_store() as (store, _):
            result = store.import_sessions(
                {"sessions": [{"id": "c_empty", "messages": []}]}
            )
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["skipped_empty"], 1)

    def test_update_session_title_and_archive(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            updated = store.update_session(session["id"], title="新标题", status="archived")
            self.assertEqual(updated["title"], "新标题")
            self.assertEqual(updated["status"], "archived")
            self.assertEqual(store.list_sessions(status="archived")[0]["id"], session["id"])


if __name__ == "__main__":
    unittest.main()
