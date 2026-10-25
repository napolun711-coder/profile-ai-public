"""OCR 单测（Phase 9）：provider 选择、外部外发边界、离线替身链路。

全部离线：需要联网的提供方要么被替换成替身，要么断言"根本没有发生网络访问"。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import ocr as ocr_service
from app.services.ocr import registry
from app.services.ocr.providers import paddle_jobs
from tests import fixtures


class ProviderSelectionTests(unittest.TestCase):
    def test_get_provider_follows_config(self) -> None:
        cases = {
            "paddle": "paddle",
            "local": "local",
            "stub": "stub",
        }
        for configured, expected in cases.items():
            with self.subTest(provider=configured):
                settings = fixtures.settings_with(ocr_provider=configured)
                self.assertEqual(ocr_service.get_provider(settings).name, expected)

    def test_unknown_provider_falls_back_to_disabled(self) -> None:
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="whatever")

        provider = ocr_service.get_provider(settings)

        self.assertEqual(provider.name, "none")
        self.assertFalse(settings.ocr_enabled)

    def test_none_provider_disables_ocr(self) -> None:
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="none", ocr_token="token")

        self.assertFalse(settings.ocr_enabled)
        self.assertFalse(settings.ocr_external)

    def test_cloud_provider_needs_token(self) -> None:
        without_token = fixtures.settings_with(ocr_mode="manual", ocr_provider="paddle")
        with_token = fixtures.settings_with(
            ocr_mode="manual", ocr_provider="paddle", ocr_token="secret"
        )

        self.assertFalse(without_token.ocr_enabled)
        self.assertTrue(with_token.ocr_enabled)
        self.assertTrue(with_token.ocr_external)

    def test_local_provider_is_not_external(self) -> None:
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="local")

        self.assertTrue(settings.ocr_enabled)
        self.assertFalse(settings.ocr_external)

    def test_provider_infos_for_settings_page(self) -> None:
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="paddle")

        infos = ocr_service.provider_infos(settings)
        names = [item["name"] for item in infos]

        self.assertEqual(names, list(registry.DISPLAY_ORDER))
        self.assertTrue(next(item for item in infos if item["name"] == "paddle")["selected"])
        self.assertTrue(next(item for item in infos if item["name"] == "paddle")["external"])
        self.assertFalse(next(item for item in infos if item["name"] == "local")["external"])


class OfflineBehaviourTests(unittest.TestCase):
    """OCR_MODE=off（默认）时不允许发生任何网络访问。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.session_mock = mock.Mock(side_effect=AssertionError("OCR 关闭时不应访问网络"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_ocr_is_noop_when_disabled(self) -> None:
        path = fixtures.make_png(self.tmp / "扫描件.png")
        settings = fixtures.settings_with()
        with mock.patch(
            "app.services.ocr.providers.paddle_jobs._get_session", self.session_mock
        ):
            outcome = ocr_service.run_ocr(path, "扫描件.png", settings=settings)

        self.assertFalse(outcome.ok)
        self.assertIn("未启用", outcome.detail)
        self.session_mock.assert_not_called()

    def test_stub_provider_returns_text(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="stub")

        outcome = ocr_service.run_ocr(path, "图.png", settings=settings)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.provider, "stub")
        self.assertIn("离线替身", outcome.text)

    def test_local_provider_rejects_pdf(self) -> None:
        path = fixtures.make_pdf(self.tmp / "扫描件.pdf", ["scan"])
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="local")

        outcome = ocr_service.run_ocr(path, "扫描件.pdf", settings=settings)

        self.assertFalse(outcome.ok)
        self.assertIn("仅支持图片", outcome.detail)

    def test_local_provider_without_dependency_gives_hint(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="local")

        outcome = ocr_service.run_ocr(path, "图.png", settings=settings)

        self.assertFalse(outcome.ok)
        self.assertIn("paddleocr", outcome.detail)

    def test_provider_exception_becomes_outcome(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="stub")
        provider = ocr_service.get_provider(settings)

        with mock.patch.object(type(provider), "run", side_effect=RuntimeError("boom")):
            outcome = ocr_service.run_with_provider(provider, path, "图.png", settings=settings)

        self.assertFalse(outcome.ok)
        self.assertIn("内部错误", outcome.detail)


class PresignedResultResponse:
    """结果下载的替身响应：`text` 按 `encoding` 解码（模拟 requests 的编码推断）。"""

    def __init__(self, status_code: int = 200, content: bytes = b"", encoding=None) -> None:
        self.status_code = status_code
        self.content = content
        self.encoding = encoding

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "latin-1", "replace")


class CloudResultDownloadTests(unittest.TestCase):
    """云端结果下载：预签名链接必须原样直连（不能带 Authorization 头）。

    实测（2026-10-01）：`resultUrl.jsonUrl` 指向百度 BOS 预签名链接
    （`...?authorization=bce-auth-v1%2F...`）；再带 `Authorization: bearer <token>` 会被 BOS
    按「请求头签名」校验并要求 date / x-bce-date，返回 400 MissingDateHeader；去掉该头即 200。
    """

    BASE = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
    PRESIGNED = (
        "https://paddleocr-store-7.bj.bcebos.com/v1/job/49645a00/json/8cedb314.json"
        "?authorization=bce-auth-v1%2FALTAKDN8%2F2026-10-01T12%3A46%3A12Z%2F604800%2Fdeadbeef"
    )
    SAME_ORIGIN = BASE + "/99123185717993472/result.json"

    def setUp(self) -> None:
        self.provider = paddle_jobs.PROVIDER
        self.settings = fixtures.settings_with(
            ocr_mode="manual",
            ocr_provider="paddle",
            ocr_token="token-for-test",
            ocr_base_url=self.BASE,
        )

    @staticmethod
    def _body(first_page: str = "第一页：中文与 English 混排", images: int = 0) -> bytes:
        extra = {"img_%d.jpg" % index: {} for index in range(images)}
        record = {
            "logId": "log-test",
            "result": {
                "layoutParsingResults": [
                    {"markdown": {"text": first_page, "images": extra}},
                    {"markdown": {"text": "第二页：扫描件补充说明", "images": {}}},
                ]
            },
        }
        return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    def _fetch(self, url: str, response: PresignedResultResponse):
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch(
            "app.services.ocr.providers.paddle_jobs._get_session", return_value=session
        ):
            outcome = self.provider._fetch_results(url, self.settings)
        return outcome, session.get.call_args.kwargs["headers"]

    def test_presigned_url_is_downloaded_without_authorization_header(self) -> None:
        outcome, headers = self._fetch(
            self.PRESIGNED, PresignedResultResponse(200, self._body(images=2))
        )
        pages, images, error = outcome

        self.assertEqual(error, "")
        self.assertEqual(pages, ["第一页：中文与 English 混排", "第二页：扫描件补充说明"])
        self.assertEqual(images, 2)
        self.assertEqual(headers.get("Accept"), "application/json")
        self.assertNotIn("Authorization", headers)   # 带了这个头就会被 BOS 判 400

    def test_same_origin_result_url_still_sends_token(self) -> None:
        (_, _, error), headers = self._fetch(
            self.SAME_ORIGIN, PresignedResultResponse(200, self._body())
        )

        self.assertEqual(error, "")
        self.assertEqual(headers.get("Authorization"), "bearer token-for-test")

    def test_download_failure_surfaces_upstream_body(self) -> None:
        body = json.dumps(
            {
                "code": "MissingDateHeader",
                "message": 'Request must have a "date" or "x-bce-date" header.',
                "requestId": "4451b4ef-be5e-41a4-af57-e3e36c25e1e4",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        (pages, images, error), _ = self._fetch(
            self.PRESIGNED, PresignedResultResponse(400, body)
        )

        self.assertEqual(pages, [])
        self.assertEqual(images, 0)
        self.assertIn("HTTP 400", error)
        self.assertIn("MissingDateHeader", error)   # 同类失败必须能自己说明原因

    def test_result_is_decoded_as_utf8(self) -> None:
        # 替身在未显式声明编码时按 latin-1 解码（等价于 requests 猜错编码），
        # 只有实现显式设置 utf-8 才能还原中文。
        (pages, _, error), _ = self._fetch(
            self.PRESIGNED,
            PresignedResultResponse(
                200, self._body(first_page="中文结果：质量门与 OCR"), encoding=None
            ),
        )

        self.assertEqual(error, "")
        self.assertEqual(pages[0], "中文结果：质量门与 OCR")


if __name__ == "__main__":
    unittest.main()
