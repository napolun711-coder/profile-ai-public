"""P0-3B2b：聊天按资料集过滤（隔离库 + 模拟上游）。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory.context import ChatContext
from app.plugins.adapters import knowledge
from app.routers import chat as chat_router
from tests.memory_fixtures import memory_store


def _cfg(*, enabled: bool):
    return replace(get_settings(), chat_topic_filter=enabled)


def _retrieve_payload(*chunks):
    return {"data": {"chunks": [{"file_path": path, "content": text} for path, text in chunks]}}


class ChatTopicFilterTests(unittest.TestCase):
    def test_topic_answer_uses_only_case_documents(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="资料集 A")
            store.set_document_topic("资料集A.md", topic["id"])
            payload = _retrieve_payload(
                ("资料集A.md", "资料集 A 的事实"),
                ("资料集B.md", "资料集 B 的事实"),
            )
            with mock.patch.object(knowledge, "get_settings", return_value=_cfg(enabled=True)), \
                 mock.patch.object(knowledge.lightrag, "retrieve", return_value=payload), \
                 mock.patch.object(knowledge.lightrag, "query", return_value={"response": "A 的答案"}) as query:
                result = knowledge.ANSWER_TOOL.invoke(query="这个资料集怎么答复", topic_id=topic["id"])

            self.assertTrue(result.ok)
            self.assertEqual(result.data["response"], "A 的答案")
            prompt = query.call_args.args[0]
            self.assertIn("资料集 A 的事实", prompt)
            self.assertNotIn("资料集 B 的事实", prompt)
            self.assertEqual(
                [item["file_path"] for item in result.data["references"]], ["资料集A.md"]
            )
            self.assertEqual([item.doc_name for item in result.evidence], ["资料集A.md"])
            self.assertEqual(query.call_args.kwargs["mode"], "bypass")

    def test_topic_without_documents_fails_closed(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="空资料集")
            with mock.patch.object(knowledge, "get_settings", return_value=_cfg(enabled=True)), \
                 mock.patch.object(knowledge.lightrag, "retrieve") as retrieve:
                result = knowledge.ANSWER_TOOL.invoke(query="这个资料集怎么答复", topic_id=topic["id"])

            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "topic_no_documents")
            retrieve.assert_not_called()

    def test_topic_without_relevant_chunks_fails_closed(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="资料集 A")
            store.set_document_topic("资料集A.md", topic["id"])
            payload = _retrieve_payload(("资料集B.md", "资料集 B 的事实"))
            with mock.patch.object(knowledge, "get_settings", return_value=_cfg(enabled=True)), \
                 mock.patch.object(knowledge.lightrag, "retrieve", return_value=payload), \
                 mock.patch.object(knowledge.lightrag, "query") as query:
                result = knowledge.ANSWER_TOOL.invoke(query="这个资料集怎么答复", topic_id=topic["id"])

            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "topic_no_evidence")
            query.assert_not_called()

    def test_switch_off_keeps_original_path(self) -> None:
        with memory_store() as (store, _):
            topic = store.create_topic(name="资料集 A")
            with mock.patch.object(knowledge, "get_settings", return_value=_cfg(enabled=False)), \
                 mock.patch.object(knowledge.lightrag, "retrieve") as retrieve, \
                 mock.patch.object(knowledge.lightrag, "query", return_value={"response": "普通答案"}) as query:
                result = knowledge.ANSWER_TOOL.invoke(query="这个资料集怎么答复", topic_id=topic["id"])

            self.assertTrue(result.ok)
            self.assertEqual(result.data["response"], "普通答案")
            retrieve.assert_not_called()
            self.assertNotEqual(query.call_args.kwargs.get("mode"), "bypass")

    def test_twenty_paired_topics_do_not_mix_answers(self) -> None:
        with memory_store() as (store, _):
            pairs = []
            for index in range(20):
                topic = store.create_topic(name="配对资料集 %02d" % (index + 1))
                file_path = "配对资料集%02d.md" % (index + 1)
                store.set_document_topic(file_path, topic["id"])
                pairs.append((topic, file_path, "事实 %02d" % (index + 1)))
            payload = _retrieve_payload(
                *[(file_path, content) for _topic, file_path, content in pairs]
            )
            mixed = 0
            with mock.patch.object(knowledge, "get_settings", return_value=_cfg(enabled=True)), \
                 mock.patch.object(knowledge.lightrag, "retrieve", return_value=payload), \
                 mock.patch.object(knowledge.lightrag, "query", return_value={"response": "答案"}) as query:
                for topic, _file_path, content in pairs:
                    result = knowledge.ANSWER_TOOL.invoke(
                        query="这个资料集怎么答复", topic_id=topic["id"]
                    )
                    if not result.ok:
                        mixed += 1
                        continue
                    prompt = query.call_args.args[0]
                    if content not in prompt:
                        mixed += 1
                    for _other_topic, _other_path, other_content in pairs:
                        if other_content != content and other_content in prompt:
                            mixed += 1
            self.assertEqual(mixed, 0)

    def test_skill_options_carry_topic_id(self) -> None:
        context = ChatContext(
            history=[],
            user_prompt="",
            local_prefix="",
            topic_id="t1",
        )
        options = chat_router._skill_options(context)
        self.assertEqual(options["topic_id"], "t1")


if __name__ == "__main__":
    unittest.main()
