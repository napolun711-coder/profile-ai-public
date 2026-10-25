"""M1：旧 localStorage 导入的规模、幂等与脱敏。"""

from __future__ import annotations

import unittest

from tests.memory_fixtures import memory_store


def _sessions(count: int) -> dict:
    items = []
    for index in range(count):
        base = 1700000000000 + index * 1000
        items.append(
            {
                "id": f"c_legacy_{index}",
                "title": f"旧会话 {index}",
                "created_at": base,
                "updated_at": base + 500,
                "messages": [
                    {"role": "user", "text": f"问题 {index}", "ts": base},
                    {"role": "ai", "text": f"回答 {index}", "ts": base + 100},
                ],
            }
        )
    return {"sessions": items}


class SessionImportTests(unittest.TestCase):
    def test_import_sixty_sessions_idempotent(self) -> None:
        with memory_store() as (store, _):
            payload = _sessions(60)
            first = store.import_sessions(payload)
            second = store.import_sessions(payload)
            self.assertEqual(first["imported"], 60)
            self.assertEqual(len(first["mapping"]), 60)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["skipped"], 60)
            self.assertEqual(store.status()["session_count"], 60)

    def test_import_redacts_credentials(self) -> None:
        with memory_store() as (store, _):
            payload = {
                "sessions": [
                    {
                        "id": "c_secret",
                        "messages": [
                            {
                                "role": "user",
                                "text": "token: abcdefghijklmnop",
                                "ts": 1700000000000,
                            }
                        ],
                    }
                ]
            }
            result = store.import_sessions(payload)
            self.assertEqual(result["redacted_messages"], 1)
            new_id = result["mapping"]["c_secret"]
            content = store.list_messages(new_id)[0]["content"]
            self.assertIn("[已隐藏的凭据]", content)
            self.assertNotIn("abcdefghijklmnop", content)

    def test_import_rejects_too_many_sessions(self) -> None:
        with memory_store() as (store, _):
            with self.assertRaises(ValueError):
                store.import_sessions(_sessions(101))

    def test_import_skips_unknown_roles_and_empty_content(self) -> None:
        with memory_store() as (store, _):
            payload = {
                "sessions": [
                    {
                        "id": "c_roles",
                        "messages": [
                            {"role": "system", "text": "系统提示"},
                            {"role": "user", "text": ""},
                            {"role": "user", "text": "唯一有效"},
                        ],
                    }
                ]
            }
            result = store.import_sessions(payload)
            self.assertEqual(result["imported"], 1)
            messages = store.list_messages(result["mapping"]["c_roles"])
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["content"], "唯一有效")


if __name__ == "__main__":
    unittest.main()
