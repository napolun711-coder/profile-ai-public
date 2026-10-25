"""项目模型硬规则：DeepSeek API + 本机 bge-m3 嵌入。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from app.core import model_policy, user_settings
from app.core.config import _build_settings


class ModelPolicyTests(unittest.TestCase):
    def test_generation_mode_env_cannot_enable_local_llm(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "GENERATION_MODE": "local",
                "LOCAL_LLM_ENABLED": "true",
            },
            clear=False,
        ):
            cfg = _build_settings()
        self.assertEqual(cfg.generation_mode, "upstream")
        self.assertFalse(cfg.llm_enabled)

    def test_to_env_forces_deepseek_and_bge_m3(self) -> None:
        data = user_settings.load()
        data["llm"].update(
            {
                "binding": "ollama",
                "model": "qwen2.5:7b",
                "host": "http://localhost:11434",
                "api_key": "test-key",
            }
        )
        data["embedding"].update(
            {
                "binding": "openai",
                "model": "other-embedding",
                "host": "https://example.invalid",
            }
        )
        data["app"]["generation_mode"] = "local"
        lightrag_env = user_settings.to_env(data, "lightrag")
        app_env = user_settings.to_env(data, "app")
        self.assertEqual(lightrag_env["LLM_BINDING"], model_policy.DEEPSEEK_BINDING)
        self.assertEqual(lightrag_env["LLM_MODEL"], model_policy.DEEPSEEK_MODEL)
        self.assertEqual(lightrag_env["LLM_BINDING_HOST"], model_policy.DEEPSEEK_BASE_URL)
        self.assertEqual(lightrag_env["EMBEDDING_BINDING"], "ollama")
        self.assertEqual(lightrag_env["EMBEDDING_MODEL"], "bge-m3:latest")
        self.assertEqual(
            lightrag_env["EMBEDDING_BINDING_HOST"],
            "http://localhost:11434",
        )
        self.assertEqual(app_env["GENERATION_MODE"], "upstream")

    def test_save_rejects_local_llm_and_other_models(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            user_settings.load(),
            {
                "llm": {
                    "binding": "ollama",
                    "model": "qwen2.5:7b",
                    "host": "http://localhost:11434",
                },
                "embedding": {
                    "binding": "openai",
                    "model": "other",
                    "host": "https://example.invalid",
                },
                "app": {"generation_mode": "local"},
            },
        )
        self.assertEqual(merged["llm"]["binding"], "openai")
        self.assertEqual(merged["llm"]["model"], "deepseek-chat")
        self.assertEqual(merged["llm"]["host"], "https://api.deepseek.com")
        self.assertEqual(merged["embedding"]["binding"], "ollama")
        self.assertEqual(merged["embedding"]["model"], "bge-m3:latest")
        self.assertEqual(merged["app"]["generation_mode"], "upstream")
        self.assertTrue(any("本机 LLM" in item for item in errors))
        self.assertTrue(any("已锁定" in item for item in errors))

    def test_settings_page_exposes_fixed_models(self) -> None:
        html = Path("static/settings.html").read_text(encoding="utf-8")
        self.assertIn('id="llm_binding" disabled', html)
        self.assertIn('value="deepseek-chat" readonly', html)
        self.assertIn('value="https://api.deepseek.com" readonly', html)
        self.assertIn('id="embedding_binding" disabled', html)
        self.assertIn('value="bge-m3:latest" readonly', html)
        self.assertIn('id="app_generation_mode" disabled', html)


if __name__ == "__main__":
    unittest.main()
