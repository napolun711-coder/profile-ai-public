"""M3-2：固定语料的质量验收（应记/不应记 + top-5 召回）。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory.extract import handle_extract_job
from tests.memory_fixtures import memory_store


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_auto_extract=True,
        memory_worker=True,
        memory_extract_backend="off",
        memory_extract_max_messages=50,
        memory_extract_max_input_tokens=4000,
    )
    return replace(base, **overrides)


SHOULD_REMEMBER = (
    ("我的项目代号是 A3", "项目代号"),
    ("项目名称是档案助手", "项目名称"),
    ("以后回答都用中文", "回答语言"),
    ("我的端口是 8100", "端口"),
    ("我们决定用 SQLite", "项目决定"),
    ("我的目标是三个月上线", "长期目标"),
    ("必须支持离线部署", "约束"),
    ("我喜欢简洁的回答", "偏好"),
)

SHOULD_NOT_REMEMBER = (
    "你好",
    "明天下午提醒我开会",
    "我的项目代号是 A3 吗？",
    "如果我的项目代号是 A3 呢",
)


class MemoryQualityTests(unittest.TestCase):
    def test_fixed_corpus_extracts_expected_active_memories(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for text, _subject in SHOULD_REMEMBER:
                store.add_user_message(session["id"], text)
            for text in SHOULD_NOT_REMEMBER:
                store.add_user_message(session["id"], text)

            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                result = handle_extract_job({"session_id": session["id"]})

            self.assertEqual(result["processed"], 12)
            active = store.memories.list(status="active", limit=100)
            subjects = {item["subject"] for item in active}
            for _text, subject in SHOULD_REMEMBER:
                self.assertIn(subject, subjects)
            self.assertEqual(len(active), len(SHOULD_REMEMBER))
            for text in SHOULD_NOT_REMEMBER:
                self.assertFalse(
                    any(text == item["content"] for item in active), text
                )

    def test_top5_recall_for_expected_subjects(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for text, _subject in SHOULD_REMEMBER:
                store.add_user_message(session["id"], text)
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                handle_extract_job({"session_id": session["id"]})

            hits = 0
            for _text, subject in SHOULD_REMEMBER:
                results = store.memories.search(subject, scope_keys=["global"], limit=5)
                if any(item["subject"] == subject for item in results):
                    hits += 1
            self.assertGreaterEqual(hits / len(SHOULD_REMEMBER), 0.8)

    def test_unrelated_query_does_not_inject_project_fact(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for text, _subject in SHOULD_REMEMBER:
                store.add_user_message(session["id"], text)
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                handle_extract_job({"session_id": session["id"]})

            from app.memory.retrieve import load_memories

            items = load_memories(store, "今天天气怎么样", budget_tokens=900)
            subjects = {item["memory"]["subject"] for item in items}
            self.assertNotIn("项目代号", subjects)
            self.assertNotIn("端口", subjects)


if __name__ == "__main__":
    unittest.main()
