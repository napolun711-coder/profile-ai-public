"""M2：设置页记忆状态骨架的静态契约。"""

from __future__ import annotations

import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS = BASE_DIR / "static" / "settings.html"


class MemoryUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = SETTINGS.read_text(encoding="utf-8")

    def test_memory_card_exists_and_is_navigable(self) -> None:
        self.assertIn('id="sec-memory"', self.html)
        self.assertIn('{ id: "sec-memory", num: "M", name: "记忆系统" }', self.html)
        self.assertIn('id="memoryState"', self.html)
        self.assertIn('id="memoryCount"', self.html)

    def test_status_api_is_wired(self) -> None:
        self.assertIn("refreshMemoryStatus()", self.html)
        self.assertIn('fetch("/api/memory/status"', self.html)
        self.assertIn("/api/memory/items", self.html)

    def test_m2_scope_is_explicit(self) -> None:
        self.assertIn("M2：原始对话持久化", self.html)
        self.assertIn("M5 记忆管理页已提供", self.html)
        self.assertIn('href="/memory"', self.html)


if __name__ == "__main__":
    unittest.main()
