"""记忆副作用开关的默认值回归：只开 MEMORY_ENABLED 不得顺带写/注入记忆。"""

from __future__ import annotations

import unittest
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SIDE_EFFECT_KEYS = (
    "MEMORY_AUTO_EXTRACT",
    "MEMORY_RECALL_ENABLED",
    "MEMORY_WORKER",
)


class MemoryDefaultSwitchTests(unittest.TestCase):
    def test_side_effects_default_off(self) -> None:
        cfg = Settings()
        self.assertFalse(cfg.memory_context_enabled)
        self.assertFalse(cfg.memory_fast_memory)
        self.assertFalse(cfg.memory_worker)

    def test_master_switch_alone_does_not_enable_side_effects(self) -> None:
        cfg = Settings(memory_enabled=True)
        self.assertFalse(cfg.memory_context_enabled)
        self.assertFalse(cfg.memory_fast_memory)
        self.assertFalse(cfg.memory_worker)

    def test_env_examples_ship_side_effects_off(self) -> None:
        name = ".env.example"
        text = (ROOT / name).read_text(encoding="utf-8")
        for key in SIDE_EFFECT_KEYS:
                self.assertIn("%s=off" % key, text, "%s 的 %s 必须为 off" % (name, key))

    def test_chat_topic_filter_default_off(self) -> None:
        self.assertFalse(Settings().chat_topic_filter)
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CHAT_TOPIC_FILTER=off", text, ".env.example 必须默认 off")


class AgentAndGraphConfigTests(unittest.TestCase):
    def test_graph_and_rag_budgets_are_explicit(self) -> None:
        cfg = Settings()
        self.assertEqual(cfg.graph_max_nodes, 3500)
        self.assertEqual(cfg.lightrag_top_k, 50)
        self.assertEqual(cfg.lightrag_chunk_top_k, 30)


if __name__ == "__main__":
    unittest.main()
