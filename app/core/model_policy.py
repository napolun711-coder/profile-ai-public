"""项目级模型硬规则。

LLM 一律使用 DeepSeek 的 OpenAI-compatible API；
本机 Ollama 只允许提供 RAG 的 bge-m3 嵌入，不允许本机 LLM 生成。
"""

from __future__ import annotations

DEEPSEEK_BINDING = "openai"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

EMBEDDING_BINDING = "ollama"
EMBEDDING_BASE_URL = "http://localhost:11434"
EMBEDDING_MODEL = "bge-m3:latest"

LOCAL_LLM_ALLOWED = False
GENERATION_MODE = "upstream"

__all__ = [
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_BINDING",
    "DEEPSEEK_MODEL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_BINDING",
    "EMBEDDING_MODEL",
    "GENERATION_MODE",
    "LOCAL_LLM_ALLOWED",
]
