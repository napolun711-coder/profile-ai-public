"""Model and RAG settings are user-configurable."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from app.core import user_settings
from app.core.config import _build_settings


class ModelPolicyTests(unittest.TestCase):
    def test_generation_mode_env_cannot_enable_local_llm(self) -> None:
        with mock.patch.dict(os.environ, {"GENERATION_MODE": "local", "LOCAL_LLM_ENABLED": "true"}, clear=False):
            cfg = _build_settings()
        self.assertEqual(cfg.generation_mode, "upstream")
        self.assertFalse(cfg.llm_enabled)

    def test_to_env_preserves_user_models_and_rag_values(self) -> None:
        data = user_settings.load()
        data["llm"].update({"binding": "ollama", "model": "qwen2.5:7b", "host": "http://localhost:11434", "api_key": "test-key"})
        data["embedding"].update({"binding": "openai", "model": "text-embedding-3-small", "host": "https://example.invalid", "api_key": "embed-key"})
        data["rag"].update({"chunk_size": "1000", "chunk_overlap": "120", "top_k": "42", "chunk_top_k": "21"})
        lightrag_env = user_settings.to_env(data, "lightrag")
        app_env = user_settings.to_env(data, "app")
        self.assertEqual(lightrag_env["LLM_BINDING"], "ollama")
        self.assertEqual(lightrag_env["LLM_MODEL"], "qwen2.5:7b")
        self.assertEqual(lightrag_env["LLM_BINDING_HOST"], "http://localhost:11434")
        self.assertEqual(lightrag_env["EMBEDDING_BINDING"], "openai")
        self.assertEqual(lightrag_env["EMBEDDING_MODEL"], "text-embedding-3-small")
        self.assertEqual(lightrag_env["EMBEDDING_BINDING_API_KEY"], "embed-key")
        self.assertEqual(lightrag_env["CHUNK_SIZE"], "1000")
        self.assertEqual(lightrag_env["CHUNK_OVERLAP_SIZE"], "120")
        self.assertEqual(app_env["LIGHTRAG_TOP_K"], "42")
        self.assertEqual(app_env["LIGHTRAG_CHUNK_TOP_K"], "21")

    def test_save_accepts_custom_models(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            user_settings.load(),
            {
                "llm": {"binding": "ollama", "model": "qwen2.5:7b", "host": "http://localhost:11434"},
                "embedding": {"binding": "openai", "model": "embed-x", "host": "https://example.invalid"},
                "rag": {"chunk_size": "1000", "top_k": "42"},
            },
        )
        self.assertFalse(errors)
        self.assertEqual(merged["llm"]["binding"], "ollama")
        self.assertEqual(merged["embedding"]["binding"], "openai")
        self.assertEqual(merged["rag"]["chunk_size"], "1000")

    def test_settings_page_exposes_editable_models_and_rag(self) -> None:
        html = Path("static/settings.html").read_text(encoding="utf-8")
        self.assertNotIn('id="llm_binding" disabled', html)
        self.assertNotIn('id="embedding_binding" disabled', html)
        for field in ("llm_model", "llm_host", "embedding_model", "embedding_host", "rag_chunk_size", "rag_chunk_overlap", "rag_top_k", "rag_chunk_top_k"):
            self.assertIn('id="' + field + '"', html)


if __name__ == "__main__":
    unittest.main()
