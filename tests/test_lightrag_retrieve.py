"""P0：LightRAG /query/data 客户端单测（全离线，FakeSession）。

守住四件事：
1. 路径与方法正确：POST /query/data，复用现有检索参数；
2. 现有 /query 的默认行为不被改变（不额外下发 stream / include_references）；
3. 输入校验在发请求之前完成；
4. 4xx / 5xx / 连不上 / 超时都收敛为 UpstreamError。
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import urlsplit
from unittest import mock

import requests

from app.clients import lightrag
from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError
from tests import fixtures


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload, ensure_ascii=False) if payload is not None else "")
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, params=None, timeout=None, **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "params": params,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("没有预置响应了：被测代码发起了多余的请求")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, json=None, timeout=None, **kwargs):
        # `lightrag.query()` 走 Session.post；与 request() 共用同一份响应队列。
        return self.request(
            "POST", url, json=json, params=None, timeout=timeout, **kwargs
        )


def path_of(url: str) -> str:
    return urlsplit(url).path


RESPONSE = {
    "status": "success",
    "message": "ok",
    "data": {
        "entities": [{"entity_name": "示例"}],
        "relationships": [],
        "chunks": [{"chunk_id": "c1", "content": "片段"}],
        "references": [{"reference_id": "r1", "file_path": "资料.md"}],
    },
    "metadata": {"query_mode": "mix"},
}


class RetrieveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = fixtures.settings_with(
            lightrag_url="http://127.0.0.1:9621",
            lightrag_top_k=40,
            lightrag_chunk_top_k=20,
            lightrag_include_references=False,
        )
        patcher = mock.patch.object(lightrag, "get_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, *responses) -> FakeSession:
        session = FakeSession(responses)
        patcher = mock.patch.object(lightrag, "get_session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_posts_to_query_data_with_existing_payload(self) -> None:
        session = self.use(FakeResponse(200, RESPONSE))

        payload = lightrag.retrieve("测试查询")

        self.assertEqual(payload, RESPONSE)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(path_of(call["url"]), "/query/data")
        self.assertEqual(call["json"]["query"], "测试查询")
        self.assertEqual(call["json"]["mode"], "mix")
        self.assertEqual(call["json"]["top_k"], 40)
        self.assertEqual(call["json"]["chunk_top_k"], 20)
        self.assertNotIn("stream", call["json"])
        self.assertNotIn("include_references", call["json"])

    def test_query_is_stripped(self) -> None:
        session = self.use(FakeResponse(200, RESPONSE))

        lightrag.retrieve("  测试查询  ")

        self.assertEqual(session.calls[0]["json"]["query"], "测试查询")

    def test_include_references_follows_setting(self) -> None:
        settings = fixtures.settings_with(lightrag_include_references=True)
        with mock.patch.object(lightrag, "get_settings", return_value=settings):
            session = self.use(FakeResponse(200, RESPONSE))
            lightrag.retrieve("测试查询")

        self.assertTrue(session.calls[0]["json"]["include_references"])

    def test_explicit_overrides(self) -> None:
        session = self.use(FakeResponse(200, RESPONSE))

        lightrag.retrieve("测试查询", top_k=12, chunk_top_k=7, mode="hybrid")

        payload = session.calls[0]["json"]
        self.assertEqual(payload["top_k"], 12)
        self.assertEqual(payload["chunk_top_k"], 7)
        self.assertEqual(payload["mode"], "hybrid")

    def test_invalid_mode_is_rejected_before_request(self) -> None:
        session = self.use()

        with self.assertRaises(ValueError):
            lightrag.retrieve("测试查询", mode="bad")

        self.assertEqual(session.calls, [])

    def test_short_query_is_rejected_before_request(self) -> None:
        session = self.use()

        for query in ("ab", "  a  ", ""):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    lightrag.retrieve(query)

        self.assertEqual(session.calls, [])

    def test_non_string_query_is_rejected(self) -> None:
        session = self.use()
        with self.assertRaises(ValueError):
            lightrag.retrieve(123)  # type: ignore[arg-type]
        self.assertEqual(session.calls, [])

    def test_invalid_top_k_is_rejected_before_request(self) -> None:
        session = self.use()
        for value in (0, -1, True, "5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    lightrag.retrieve("测试查询", top_k=value)  # type: ignore[arg-type]
        self.assertEqual(session.calls, [])

    def test_invalid_chunk_top_k_is_rejected_before_request(self) -> None:
        session = self.use()
        for value in (0, -1, True, "5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    lightrag.retrieve("测试查询", chunk_top_k=value)  # type: ignore[arg-type]
        self.assertEqual(session.calls, [])

    def test_upstream_4xx_becomes_upstream_error(self) -> None:
        self.use(FakeResponse(422, None, text='{"detail":"query too short"}'))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.retrieve("测试查询")

        self.assertEqual(ctx.exception.upstream_status, 422)
        self.assertIn("query too short", str(ctx.exception.upstream_body))

    def test_upstream_5xx_becomes_upstream_error(self) -> None:
        self.use(FakeResponse(503, None, text="gateway down"))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.retrieve("测试查询")

        self.assertEqual(ctx.exception.upstream_status, 503)
        self.assertIn("503", ctx.exception.message)

    def test_connection_error_becomes_upstream_error(self) -> None:
        self.use(requests.exceptions.ConnectionError("refused"))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.retrieve("测试查询")

        self.assertIn("知识库服务不可用", ctx.exception.message)

    def test_timeout_becomes_timeout_error(self) -> None:
        self.use(requests.exceptions.Timeout("slow"))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.retrieve("测试查询")

        self.assertEqual(ctx.exception.kind, UPSTREAM_TIMEOUT)
        self.assertEqual(ctx.exception.status_code, 504)


class QueryReferenceTests(unittest.TestCase):
    """P2B：`/query` 的可选 include_references 不改变现有默认行为。"""

    def setUp(self) -> None:
        self.settings = fixtures.settings_with(
            lightrag_url="http://127.0.0.1:9621",
            lightrag_include_references=False,
        )
        patcher = mock.patch.object(lightrag, "get_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, *responses) -> FakeSession:
        session = FakeSession(responses)
        patcher = mock.patch.object(lightrag, "get_session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def test_default_does_not_include_references(self) -> None:
        session = self.use(FakeResponse(200, {"response": "答案"}))

        lightrag.query("测试查询")

        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(path_of(call["url"]), "/query")
        self.assertNotIn("include_references", call["json"])

    def test_explicit_true_includes_references(self) -> None:
        session = self.use(FakeResponse(200, {"response": "答案"}))

        lightrag.query("测试查询", include_references=True)

        self.assertTrue(session.calls[0]["json"]["include_references"])

    def test_explicit_false_overrides_setting(self) -> None:
        settings = fixtures.settings_with(lightrag_include_references=True)
        with mock.patch.object(lightrag, "get_settings", return_value=settings):
            session = self.use(FakeResponse(200, {"response": "答案"}))
            lightrag.query("测试查询", include_references=False)

        self.assertNotIn("include_references", session.calls[0]["json"])

    def test_invalid_include_references_is_rejected_before_request(self) -> None:
        session = self.use()

        for value in ("true", 1, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    lightrag.query("测试查询", include_references=value)  # type: ignore[arg-type]

        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
