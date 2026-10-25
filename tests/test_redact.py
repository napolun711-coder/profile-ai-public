"""M1：持久化前的凭据脱敏。"""

from __future__ import annotations

import unittest

from app.memory.redact import REPLACEMENT, is_secret_like, redact_text


class RedactTests(unittest.TestCase):
    def test_api_key(self) -> None:
        text, mark = redact_text("key=sk-abcdefghijklmnopqrstuvwxyz123456")
        self.assertIn(REPLACEMENT, text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", text)
        self.assertTrue(mark["redacted"])
        self.assertIn("api_key", mark["types"])
        self.assertTrue(is_secret_like(text.replace(REPLACEMENT, "sk-abcdefghijklmnopqrstuvwxyz123456")))

    def test_bearer(self) -> None:
        text, mark = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456")
        self.assertIn(REPLACEMENT, text)
        self.assertTrue(mark["redacted"])

    def test_assignment_keeps_label(self) -> None:
        text, mark = redact_text("password: hunter2hunter2")
        self.assertIn("password=", text)
        self.assertIn(REPLACEMENT, text)
        self.assertNotIn("hunter2hunter2", text)
        self.assertTrue(mark["redacted"])

    def test_private_key_block(self) -> None:
        block = (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
            "-----END PRIVATE KEY-----"
        )
        text, mark = redact_text(block)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", text)
        self.assertTrue(mark["redacted"])

    def test_normal_text_untouched(self) -> None:
        text, mark = redact_text("我的项目代号是 A3，端口 8100。回答用中文。")
        self.assertEqual(text, "我的项目代号是 A3，端口 8100。回答用中文。")
        self.assertFalse(mark["redacted"])


if __name__ == "__main__":
    unittest.main()
