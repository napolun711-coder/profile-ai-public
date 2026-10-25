"""M1：/api/sessions 路由函数（不启动 HTTP 服务，直接调用）。"""

from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.memory import runtime
from app.models.schemas import SessionCreate, SessionImport, SessionUpdate
from app.routers import sessions as sessions_api
from tests.memory_fixtures import memory_store


class SessionsApiTests(unittest.TestCase):
    def test_disabled_returns_empty_list_and_503_on_write(self) -> None:
        runtime.reset_for_tests(enabled=False)
        try:
            listed = sessions_api.list_sessions(limit=50, offset=0, status="active")
            self.assertFalse(listed["enabled"])
            self.assertEqual(listed["sessions"], [])
            with self.assertRaises(HTTPException) as caught:
                sessions_api.create_session(SessionCreate())
            self.assertEqual(caught.exception.status_code, 503)
        finally:
            runtime.reset_for_tests()

    def test_crud_and_messages(self) -> None:
        with memory_store() as (store, _):
            created = sessions_api.create_session(SessionCreate(title="第一段"))
            session = created["session"]
            self.assertTrue(session["id"].startswith("s_"))
            self.assertEqual(session["title"], "第一段")

            store.add_user_message(session["id"], "问题一", client_message_id="a1")
            store.add_assistant_message(
                session["id"], content="回答一", status="complete"
            )

            listed = sessions_api.list_sessions(limit=50, offset=0, status="active")
            self.assertTrue(listed["enabled"])
            self.assertEqual(len(listed["sessions"]), 1)
            self.assertEqual(listed["sessions"][0]["message_count"], 2)

            detail = sessions_api.get_session(session["id"])["session"]
            self.assertEqual(detail["id"], session["id"])

            messages = sessions_api.list_messages(
                session["id"], limit=200, before_seq=None
            )["messages"]
            self.assertEqual([item["role"] for item in messages], ["user", "ai"])
            self.assertEqual(messages[0]["text"], "问题一")

            updated = sessions_api.update_session(
                session["id"], SessionUpdate(title="改名", status="archived")
            )["session"]
            self.assertEqual(updated["title"], "改名")
            self.assertEqual(updated["status"], "archived")

            exported = sessions_api.export_session(session["id"])
            self.assertEqual(exported["session"]["id"], session["id"])
            self.assertEqual(len(exported["messages"]), 2)

    def test_clear_delete_and_404(self) -> None:
        with memory_store() as (store, _):
            session = sessions_api.create_session(SessionCreate())["session"]
            store.add_user_message(session["id"], "会被清空")
            cleared = sessions_api.clear_messages(session["id"])
            self.assertEqual(cleared["deleted"], 1)
            self.assertEqual(
                sessions_api.list_messages(session["id"], limit=200, before_seq=None)[
                    "messages"
                ],
                [],
            )

            deleted = sessions_api.delete_session(session["id"], purge=False)
            self.assertTrue(deleted["ok"])
            with self.assertRaises(HTTPException) as caught:
                sessions_api.get_session(session["id"])
            self.assertEqual(caught.exception.status_code, 404)

    def test_import_via_api(self) -> None:
        with memory_store() as (store, _):
            result = sessions_api.import_sessions(
                SessionImport(
                    sessions=[
                        {
                            "id": "c_old",
                            "title": "旧对话",
                            "created_at": 1700000000000,
                            "updated_at": 1700000001000,
                            "messages": [
                                {"role": "user", "text": "旧问题", "ts": 1700000000000},
                                {"role": "ai", "text": "旧回答", "ts": 1700000001000},
                            ],
                        }
                    ]
                )
            )
            self.assertEqual(result["imported"], 1)
            new_id = result["mapping"]["c_old"]
            self.assertEqual(
                sessions_api.list_messages(new_id, limit=200, before_seq=None)[
                    "messages"
                ][1]["text"],
                "旧回答",
            )

    def test_end_session_touches(self) -> None:
        with memory_store() as (store, _):
            session = sessions_api.create_session(SessionCreate())["session"]
            result = sessions_api.end_session(session["id"])
            self.assertTrue(result["ok"])
            self.assertFalse(result["extraction_queued"])


if __name__ == "__main__":
    unittest.main()
