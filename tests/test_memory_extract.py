"""M3-2：Tier 2 提取器、JSON 校验与 handler。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory.extract import (
    MemoryExtractor,
    collect_rule_candidates,
    handle_extract_job,
    parse_llm_candidates,
)
from app.memory.worker import JobRetry, MemoryJobWorker
from tests.memory_fixtures import memory_store


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_auto_extract=True,
        memory_worker=True,
        memory_extract_backend="off",
        memory_extract_every_turns=6,
        memory_extract_max_messages=20,
        memory_extract_max_input_tokens=4000,
        memory_extract_debounce_seconds=300,
        memory_cloud_extract=False,
        llm_enabled=False,
    )
    return replace(base, **overrides)


def _messages(*pairs):
    return [
        {"id": message_id, "seq": index + 1, "content": content}
        for index, (message_id, content) in enumerate(pairs)
    ]


class RuleCandidateTests(unittest.TestCase):
    def test_stable_fact_has_source_and_active_status(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(session["id"], "我的项目代号是 A3")
            candidates = collect_rule_candidates(
                [
                    {
                        "id": message["id"],
                        "seq": message["seq"],
                        "content": message["content"],
                    }
                ],
                session_id=session["id"],
            )
            self.assertTrue(candidates)
            candidate = candidates[0]
            self.assertEqual(candidate["subject"], "项目代号")
            self.assertEqual(candidate["status"], "active")
            self.assertEqual(candidate["message_id"], message["id"])
            self.assertIn(candidate["quote"], message["content"])

    def test_temporary_question_and_secret_are_skipped(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            candidates = collect_rule_candidates(
                _messages(
                    ("m1", "明天下午提醒我开会"),
                    ("m2", "我的项目代号是 A3 吗？"),
                    ("m3", "我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz123456"),
                ),
                session_id=session["id"],
            )
            self.assertEqual(candidates, [])


class LlmParseTests(unittest.TestCase):
    def test_valid_candidate_becomes_pending_model_inferred(self) -> None:
        messages = _messages(("m1", "我的项目代号是 A3"))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "项目代号是 A3",
                        "subject": "项目代号",
                        "kind": "fact",
                        "confidence": 0.9,
                        "sensitivity": "normal",
                        "message_id": "m1",
                        "quote": "我的项目代号是 A3",
                        "reason": "stable",
                    }
                ]
            }
        )
        candidates = parse_llm_candidates(
            text, messages=messages, session_id="s1"
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "pending")
        self.assertEqual(candidates[0]["source_type"], "model_inferred")

    def test_bad_quote_secret_and_unknown_message_are_dropped(self) -> None:
        messages = _messages(("m1", "我的项目代号是 A3"))
        text = json.dumps(
            {
                "memories": [
                    {
                        "content": "项目代号是 A3",
                        "subject": "项目代号",
                        "kind": "fact",
                        "message_id": "m1",
                        "quote": "不存在的引用",
                    },
                    {
                        "content": "秘密",
                        "subject": "秘密",
                        "kind": "fact",
                        "sensitivity": "secret",
                        "message_id": "m1",
                        "quote": "我的项目代号是 A3",
                    },
                    {
                        "content": "另一条",
                        "subject": "另一条",
                        "kind": "fact",
                        "message_id": "m9",
                        "quote": "我的项目代号是 A3",
                    },
                ]
            }
        )
        self.assertEqual(
            parse_llm_candidates(text, messages=messages, session_id="s1"), []
        )

    def test_code_fence_is_tolerated(self) -> None:
        messages = _messages(("m1", "我的项目代号是 A3"))
        body = json.dumps(
            {
                "memories": [
                    {
                        "content": "项目代号是 A3",
                        "subject": "项目代号",
                        "kind": "fact",
                        "message_id": "m1",
                        "quote": "我的项目代号是 A3",
                    }
                ]
            }
        )
        candidates = parse_llm_candidates(
            "```json\n" + body + "\n```",
            messages=messages,
            session_id="s1",
        )
        self.assertEqual(len(candidates), 1)

    def test_bad_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_llm_candidates(
                "not json", messages=_messages(("m1", "x")), session_id="s"
            )


class ExtractorTests(unittest.TestCase):
    def test_llm_error_falls_back_to_rule_candidates(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            extractor = MemoryExtractor(
                llm_call=lambda _prompt: (_ for _ in ()).throw(RuntimeError("boom"))
            )
            batch = extractor.extract(
                _messages(("m1", "我的项目代号是 A3")),
                [],
                session_id=session["id"],
            )
            self.assertEqual(batch.llm_error, "RuntimeError")
            self.assertEqual(batch.rule_count, 1)
            self.assertTrue(batch.candidates)
            self.assertEqual(batch.candidates[0]["extractor"], "rule")

    def test_llm_candidates_are_appended_pending(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            response = json.dumps(
                {
                    "memories": [
                        {
                            "content": "偏好简短回答",
                            "subject": "回答风格",
                            "kind": "preference",
                            "message_id": "m1",
                            "quote": "我喜欢简短回答",
                        }
                    ]
                }
            )
            batch = MemoryExtractor(llm_call=lambda _prompt: response).extract(
                _messages(("m1", "我喜欢简短回答")),
                [],
                session_id=session["id"],
            )
            llm_items = [
                item for item in batch.candidates if item["extractor"] == "llm"
            ]
            self.assertEqual(len(llm_items), 1)
            self.assertEqual(llm_items[0]["status"], "pending")
            self.assertEqual(llm_items[0]["source_type"], "model_inferred")


class ExtractHandlerTests(unittest.TestCase):
    def test_handler_extracts_and_advances_watermark(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(session["id"], "我的项目代号是 A3")
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                result = handle_extract_job({"session_id": session["id"]})
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["watermark"], message["seq"])
            self.assertEqual(
                store.jobs.get_watermark(session["id"]), message["seq"]
            )
            active = store.memories.list(status="active")
            self.assertTrue(any("A3" in item["content"] for item in active))

    def test_handler_is_idempotent_after_watermark(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "我的项目代号是 A3")
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                handle_extract_job({"session_id": session["id"]})
                second = handle_extract_job({"session_id": session["id"]})
            self.assertEqual(second["processed"], 0)
            self.assertEqual(len(store.memories.list(status="active")), 1)

    def test_handler_advances_watermark_when_only_assistant_messages(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            assistant = store.add_assistant_message(
                session["id"], content="我的项目代号是 A3", status="complete"
            )
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                result = handle_extract_job({"session_id": session["id"]})
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["watermark"], assistant["seq"])
            self.assertEqual(store.memories.list(status="active"), [])

    def test_handler_llm_error_with_rules_writes_pending_and_advances(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(session["id"], "我的项目代号是 A3")
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                result = handle_extract_job(
                    {"session_id": session["id"]},
                    llm_call=lambda _prompt: (_ for _ in ()).throw(
                        RuntimeError("boom")
                    ),
                )
            self.assertEqual(result["llm_error"], "RuntimeError")
            self.assertEqual(result["watermark"], message["seq"])
            pending = store.memories.list(status="pending")
            self.assertEqual(len(pending), 1)
            self.assertEqual(store.memories.list(status="active"), [])

    def test_handler_llm_error_without_rules_retries(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            store.add_user_message(session["id"], "今天天气怎么样")
            with mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                with self.assertRaises(JobRetry):
                    handle_extract_job(
                        {"session_id": session["id"]},
                        llm_call=lambda _prompt: (_ for _ in ()).throw(
                            RuntimeError("boom")
                        ),
                    )
            self.assertEqual(store.jobs.get_watermark(session["id"]), 0)

    def test_handler_respects_max_messages(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            first = store.add_user_message(session["id"], "我的项目代号是 A3")
            store.add_user_message(session["id"], "我的端口是 8100")
            with mock.patch(
                "app.memory.extract.get_settings",
                return_value=_cfg(memory_extract_max_messages=1),
            ):
                result = handle_extract_job({"session_id": session["id"]})
            self.assertEqual(result["processed"], 1)
            self.assertEqual(result["watermark"], first["seq"])
            self.assertEqual(len(store.memories.list(status="active")), 1)

    def test_worker_registered_handler_completes_job(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(session["id"], "我的项目代号是 A3")
            job = store.jobs.enqueue(
                kind="extract_memory", session_id=session["id"]
            )
            worker = MemoryJobWorker(worker_id="w-extract")
            worker.register("extract_memory", handle_extract_job)
            with mock.patch(
                "app.memory.worker.get_settings", return_value=_cfg()
            ), mock.patch(
                "app.memory.extract.get_settings", return_value=_cfg()
            ):
                self.assertTrue(worker.run_once())
            done = store.jobs.get(job["id"])
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["result"]["watermark"], message["seq"])
            self.assertTrue(store.memories.list(status="active"))


if __name__ == "__main__":
    unittest.main()
