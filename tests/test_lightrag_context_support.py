"""M2：LightRAG conversation_history/user_prompt 请求体与能力探测。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.clients import lightrag
from app.core.config import get_settings


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _openapi(properties):
    return {
        "components": {
            "schemas": {
                "QueryRequest": {"properties": properties},
            }
        }
    }


class QueryPayloadTests(unittest.TestCase):
    def test_payload_without_context_unchanged(self) -> None:
        payload = lightrag._query_payload("问题", stream=True)
        self.assertNotIn("conversation_history", payload)
        self.assertNotIn("user_prompt", payload)

    def test_payload_includes_history_and_prompt(self) -> None:
        payload = lightrag._query_payload(
            "问题",
            stream=True,
            conversation_history=[
                {"role": "user", "content": "上一轮"},
                {"role": "assistant", "content": "上一轮回答"},
                {"role": "system", "content": "应被过滤"},
                {"role": "user", "content": ""},
            ],
            user_prompt="## 长期记忆\n- 回答语言：中文",
        )
        self.assertEqual(
            payload["conversation_history"],
            [
                {"role": "user", "content": "上一轮"},
                {"role": "assistant", "content": "上一轮回答"},
            ],
        )
        self.assertIn("回答语言", payload["user_prompt"])


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        lightrag.reset_query_context_support_cache()

    def tearDown(self) -> None:
        lightrag.reset_query_context_support_cache()

    def test_probe_true(self) -> None:
        response = FakeResponse(
            200, _openapi({"conversation_history": {}, "user_prompt": {}})
        )
        with mock.patch.object(lightrag.requests, "get", return_value=response):
            self.assertTrue(lightrag.probe_query_context_support(force=True))

    def test_probe_false_when_fields_missing(self) -> None:
        response = FakeResponse(200, _openapi({"query": {}}))
        with mock.patch.object(lightrag.requests, "get", return_value=response):
            self.assertFalse(lightrag.probe_query_context_support(force=True))

    def test_probe_failure_degrades(self) -> None:
        with mock.patch.object(
            lightrag.requests, "get", side_effect=RuntimeError("down")
        ):
            self.assertIsNone(lightrag.probe_query_context_support(force=True))
            cfg = replace(get_settings(), memory_upstream_history="auto")
            with mock.patch.object(lightrag, "get_settings", return_value=cfg):
                self.assertFalse(lightrag.supports_query_context())

    def test_mode_on_off(self) -> None:
        with mock.patch.object(
            lightrag, "get_settings", return_value=replace(get_settings(), memory_upstream_history="on")
        ), mock.patch.object(lightrag, "probe_query_context_support") as probe:
            self.assertTrue(lightrag.supports_query_context())
            probe.assert_not_called()
        with mock.patch.object(
            lightrag, "get_settings", return_value=replace(get_settings(), memory_upstream_history="off")
        ), mock.patch.object(lightrag, "probe_query_context_support") as probe:
            self.assertFalse(lightrag.supports_query_context())
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
