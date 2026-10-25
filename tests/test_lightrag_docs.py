"""Phase 16：文档管理客户端（app/clients/lightrag.py 新增部分）的离线用例。

守住三件事：
1. **调用方式**与 LightRAG 1.5.6 的 OpenAPI 一致：
   `/documents/paginated` 是 POST + JSON body；`/documents/delete_document` 是 DELETE + JSON body；
   `page_size` 会被夹进上游允许的区间（实测 <10 会被 422 拒绝，>200 没必要）；
2. **读取类**接口遇到 4xx / 5xx / 连不上都抛 `UpstreamError`
   （页面据此显示「知识库没连上」并降级为本机视图）；
3. **删除**接口把 4xx 原样交回调用方（「管线忙 / 找不到该文档」是业务结论，
   不是服务故障，必须让路由去说人话），只有 5xx 与网络异常才抛错。

全程离线：HTTP 会话换成 `FakeSession`，不发任何真实请求。
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import urlsplit
from unittest import mock

import requests

from app.clients import lightrag
from app.core.errors import UpstreamError


class FakeResponse:
    """最小响应替身（够 lightrag 客户端用：status_code / text / json() / close()）。"""

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
    """记录每次调用，并按顺序吐出预置响应（用完了再调用会报错）。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, params=None, timeout=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "json": json, "params": params, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("没有预置响应了：被测代码发起了多余的请求")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def path_of(url: str) -> str:
    return urlsplit(url).path


class ClientFixture(unittest.TestCase):
    """把会话换成替身；其余（超时、异常包装）走的都是真实实现。"""

    def use(self, *responses) -> FakeSession:
        session = FakeSession(responses)
        patcher = mock.patch.object(lightrag, "get_session", return_value=session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session


class PaginatedTests(ClientFixture):
    """POST /documents/paginated（分页列出文档）。"""

    def test_posts_json_body_with_expected_fields(self) -> None:
        session = self.use(
            FakeResponse(200, {"documents": [], "pagination": {}, "status_counts": {}})
        )

        payload = lightrag.documents_paginated(page=2, page_size=50)

        self.assertEqual(payload, {"documents": [], "pagination": {}, "status_counts": {}})
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(path_of(call["url"]), "/documents/paginated")
        self.assertEqual(call["json"]["page"], 2)
        self.assertEqual(call["json"]["page_size"], 50)
        self.assertEqual(call["json"]["sort_field"], "updated_at")
        self.assertEqual(call["json"]["sort_direction"], "desc")
        self.assertNotIn("status_filter", call["json"])

    def test_page_size_is_clamped_into_upstream_range(self) -> None:
        session = self.use(
            FakeResponse(200, {"documents": []}), FakeResponse(200, {"documents": []})
        )

        lightrag.documents_paginated(page_size=3)
        lightrag.documents_paginated(page_size=9999)

        self.assertEqual(session.calls[0]["json"]["page_size"], lightrag.DOCUMENT_MIN_PAGE_SIZE)
        self.assertEqual(session.calls[1]["json"]["page_size"], lightrag.DOCUMENT_MAX_PAGE_SIZE)

    def test_page_and_direction_are_normalized(self) -> None:
        session = self.use(FakeResponse(200, {"documents": []}))

        lightrag.documents_paginated(page=0, sort_direction="ASC")

        self.assertEqual(session.calls[0]["json"]["page"], 1)
        self.assertEqual(session.calls[0]["json"]["sort_direction"], "asc")

    def test_status_filter_only_accepts_known_statuses(self) -> None:
        session = self.use(
            FakeResponse(200, {"documents": []}), FakeResponse(200, {"documents": []})
        )

        lightrag.documents_paginated(status="failed")
        lightrag.documents_paginated(status="whatever")

        self.assertEqual(session.calls[0]["json"]["status_filter"], "failed")
        self.assertNotIn("status_filter", session.calls[1]["json"])

    def test_status_list_matches_lightrag_enum(self) -> None:
        # 上游枚举变了（或我们写错）时这条会先炸，而不是等用户看到「未知状态」
        self.assertEqual(
            set(lightrag.DOCUMENT_STATUSES),
            {
                "pending",
                "parsing",
                "analyzing",
                "processing",
                "preprocessed",
                "processed",
                "failed",
            },
        )


class ReadOnlyTests(ClientFixture):
    """只读接口的路径与错误行为。"""

    def test_status_counts_gets_expected_path(self) -> None:
        session = self.use(FakeResponse(200, {"status_counts": {"processed": 3, "all": 3}}))

        payload = lightrag.status_counts()

        self.assertEqual(payload["status_counts"]["processed"], 3)
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(path_of(session.calls[0]["url"]), "/documents/status_counts")

    def test_pipeline_status_gets_expected_path(self) -> None:
        session = self.use(FakeResponse(200, {"busy": False, "latest_message": "idle"}))

        payload = lightrag.pipeline_status()

        self.assertFalse(payload["busy"])
        self.assertEqual(path_of(session.calls[0]["url"]), "/documents/pipeline_status")

    def test_upstream_4xx_raises_with_status_and_body(self) -> None:
        self.use(FakeResponse(404, None, text='{"detail":"not found"}'))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.status_counts()

        self.assertEqual(ctx.exception.upstream_status, 404)
        self.assertIn("not found", str(ctx.exception.upstream_body))

    def test_upstream_5xx_raises(self) -> None:
        self.use(FakeResponse(503, None, text="gateway down"))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.documents_paginated()

        self.assertEqual(ctx.exception.upstream_status, 503)
        self.assertIn("503", ctx.exception.message)

    def test_connection_error_becomes_upstream_error(self) -> None:
        self.use(requests.exceptions.ConnectionError("refused"))

        with self.assertRaises(UpstreamError) as ctx:
            lightrag.pipeline_status()

        self.assertIn("知识库服务不可用", ctx.exception.message)


class DeleteTests(ClientFixture):
    """DELETE /documents/delete_document：4xx 交回调用方，5xx 才抛错。"""

    def test_delete_sends_json_body(self) -> None:
        session = self.use(FakeResponse(200, {"status": "deletion_started", "doc_id": "doc-1"}))

        response = lightrag.delete_document(["doc-1"], delete_file=True, delete_llm_cache=False)

        self.assertEqual(response.status_code, 200)
        call = session.calls[0]
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(path_of(call["url"]), "/documents/delete_document")
        self.assertEqual(
            call["json"],
            {"doc_ids": ["doc-1"], "delete_file": True, "delete_llm_cache": False},
        )

    def test_blank_doc_ids_are_dropped(self) -> None:
        session = self.use(FakeResponse(200, {"status": "ok"}))

        lightrag.delete_document(["doc-1", "", "  "])

        self.assertEqual(session.calls[0]["json"]["doc_ids"], ["doc-1"])

    def test_default_scope_does_not_touch_upstream_files(self) -> None:
        session = self.use(FakeResponse(200, {"status": "ok"}))

        lightrag.delete_document(["doc-1"])

        self.assertFalse(session.calls[0]["json"]["delete_file"])
        self.assertFalse(session.calls[0]["json"]["delete_llm_cache"])

    def test_409_is_returned_to_caller_not_raised(self) -> None:
        # 「管线忙」是业务结论：必须能拿到 status_code 与原文去说人话
        self.use(FakeResponse(409, None, text='{"detail":"Pipeline is busy"}'))

        response = lightrag.delete_document(["doc-1"])

        self.assertEqual(response.status_code, 409)
        self.assertIn("busy", response.text)

    def test_404_is_returned_to_caller(self) -> None:
        self.use(FakeResponse(404, None, text='{"detail":"not found"}'))

        response = lightrag.delete_document(["gone"])

        self.assertEqual(response.status_code, 404)

    def test_5xx_raises_upstream_error(self) -> None:
        self.use(FakeResponse(500, None, text="boom"))

        with self.assertRaises(UpstreamError):
            lightrag.delete_document(["doc-1"])


class DocumentsByNameTests(ClientFixture):
    """按文档名反查 doc_id（删除时补 doc_id 用）。"""

    def test_matches_case_insensitively_and_stops_at_last_page(self) -> None:
        session = self.use(
            FakeResponse(
                200,
                {
                    "documents": [{"id": "doc-a", "file_path": "资料.md"}],
                    "pagination": {"page": 1, "has_next": True},
                },
            ),
            FakeResponse(
                200,
                {
                    "documents": [{"id": "doc-b", "file_path": "other.md"}],
                    "pagination": {"page": 2, "has_next": False},
                },
            ),
        )

        found = lightrag.documents_by_name("资料.MD")

        self.assertEqual([item["id"] for item in found], ["doc-a"])
        self.assertEqual(len(session.calls), 2)

    def test_empty_name_returns_without_any_request(self) -> None:
        session = self.use()

        self.assertEqual(lightrag.documents_by_name(""), [])
        self.assertEqual(session.calls, [])


class ClampTests(unittest.TestCase):
    def test_clamp_page_size(self) -> None:
        self.assertEqual(lightrag.clamp_page_size(0), lightrag.DOCUMENT_MIN_PAGE_SIZE)
        self.assertEqual(lightrag.clamp_page_size("abc"), lightrag.DOCUMENT_MIN_PAGE_SIZE)
        self.assertEqual(lightrag.clamp_page_size(10), 10)
        self.assertEqual(lightrag.clamp_page_size(200), 200)
        self.assertEqual(lightrag.clamp_page_size(10_000), lightrag.DOCUMENT_MAX_PAGE_SIZE)


if __name__ == "__main__":
    unittest.main()