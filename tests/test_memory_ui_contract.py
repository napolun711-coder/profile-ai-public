"""Memory status stays visible in the simplified settings page."""

from __future__ import annotations

import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS = BASE_DIR / "static" / "settings.html"


class MemoryUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = SETTINGS.read_text(encoding="utf-8")

    def test_memory_is_part_of_access_section(self) -> None:
        self.assertIn('id="sec-access"', self.html)
        self.assertIn('id="memoryState"', self.html)
        self.assertIn('id="memoryCount"', self.html)
        self.assertIn('href="/memory"', self.html)

    def test_status_api_is_wired(self) -> None:
        self.assertIn("refreshMemoryStatus()", self.html)
        self.assertIn('"/api/memory/status"', self.html)
        self.assertIn("/api/memory/items", self.html)


if __name__ == "__main__":
    unittest.main()
