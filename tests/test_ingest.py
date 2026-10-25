"""入库编排单测（Phase 8）：验证"未通过质检的文件绝不出现在知识库"。

做法：把落盘目录指向临时目录，把 LightRAG 上传接口与 OCR 会话换成替身，
因此整个用例离线运行，且能断言「有没有外发」「有没有入库」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import ingest
from tests import fixtures

REPEATED_LINES = "\n".join(["同样的一行内容"] * 60)


class IngestTestCase(unittest.TestCase):
    """公共装置：临时目录 + 替身上游 + 隔离配置。"""

    def setUp(self) -> None:
        # Windows 上后台线程可能仍在写文件，忽略清理失败以免掩盖真实断言
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)

        self.parsed_dir = self.tmp / "parsed"
        self.knowledge_dir = self.tmp / "knowledge"
        self.parsed_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

        self.upload_mock = mock.Mock(return_value=fixtures.FakeResponse())
        # OCR_MODE=off 时不允许发生任何网络访问
        self.ocr_session_mock = mock.Mock(side_effect=AssertionError("OCR 关闭时不应访问网络"))

        self._patchers = [
            mock.patch.object(ingest, "PARSED_DIR", self.parsed_dir),
            mock.patch.object(ingest, "KNOWLEDGE_DIR", self.knowledge_dir),
            mock.patch.object(ingest.lightrag, "upload", self.upload_mock),
            mock.patch(
                "app.services.ocr.providers.paddle_jobs._get_session", self.ocr_session_mock
            ),
            *fixtures.patch_settings(),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---------- 辅助 ----------

    def submit(self, filename: str, path: Path) -> str:
        data = ingest.create_job(
            filename=filename,
            content=path.read_bytes(),
            content_type="application/octet-stream",
        )
        return data["file_id"]

    def enable_stub_ocr(self, **overrides) -> None:
        """开启「离线替身 OCR」（OCR_MODE=manual + provider=stub，不外发）。

        用于验证「图片识别 → 回填 → 复检 → 入库」全链路，不需要任何外部服务。
        """
        options = {"ocr_mode": "manual", "ocr_provider": "stub"}
        options.update(overrides)
        for patcher in fixtures.patch_settings(**options):
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_gate(self) -> None:
        """把入库确认门设为 off（流水线用例专用）。

        确认门本身（always / auto / off / 会话级「全部允许」四种走法）由
        tests/test_confirm.py 覆盖；这一文件关心的是「解析 → 质检 → 入库」链路
        本身跑通没有，所以按「用户明确关掉了这道门」的方式放行，
        免得每个旧用例都要先点一次「入库」。
        """
        for patcher in fixtures.patch_settings(ingest_confirm="off"):
            patcher.start()
            self.addCleanup(patcher.stop)

    def result_json(self, file_id: str) -> dict:
        path = ingest.result_path(file_id)
        self.assertTrue(path.is_file(), "result.json 应该存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def wait_for_state(self, file_id: str, expected: str, timeout: float = 20.0) -> dict:
        """等待后台工作线程把作业推进到指定状态（验证队列链路）。

        注意：不能只等"进入终态"，因为 blocked 也是终态，会与上一次的结果混淆。
        """
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = ingest.read_result(file_id) or {}
            if data.get("state") == expected:
                return data
            time.sleep(0.05)
        self.fail(f"作业在 {timeout} 秒内没有进入 {expected}：{ingest.read_result(file_id)}")
        return {}


class IngestPipelineTests(IngestTestCase):
    def test_pipeline_stops_at_confirm_gate_by_default(self) -> None:
        """Phase 15 默认行为：质检通过 ≠ 入库，先停在确认门等用户点头。"""
        path = fixtures.make_text(
            self.tmp / "待确认的总结.md",
            "# 工作总结\n\n本季度完成了资料库建设与问答验证，共整理 42 份资料。\n"
            "下一步计划补充历史归档，并优化检索效果。\n",
        )
        file_id = self.submit("待确认的总结.md", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_REVIEW)
        self.upload_mock.assert_not_called()
        result = self.result_json(file_id)
        self.assertEqual(result["quality"]["level"], "pass")
        self.assertTrue(result["review_since"])
        self.assertEqual(result["advice"]["mode"], ingest.CONFIRM_ALWAYS)

    def test_normal_text_is_ingested(self) -> None:
        self.open_gate()
        path = fixtures.make_text(
            self.tmp / "工作总结.md",
            "# 工作总结\n\n本季度完成了资料库建设与问答验证，共整理 42 份资料。\n"
            "下一步计划补充历史归档，并优化检索效果。\n",
        )
        file_id = self.submit("工作总结.md", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)

        name, payload, content_type = self.upload_mock.call_args[0]
        self.assertEqual(name, "工作总结.md")
        self.assertEqual(content_type, "text/markdown")
        self.assertIn("本季度完成了资料库建设", payload.decode("utf-8"))

        result = self.result_json(file_id)
        self.assertEqual(result["quality"]["level"], "pass")
        self.assertEqual(result["lightrag"]["track_id"], "track-test")
        self.assertEqual(result["state"], "done")

        knowledge = list(self.knowledge_dir.glob("*.md"))
        self.assertEqual(len(knowledge), 1)

    def test_empty_text_is_blocked_and_not_ingested(self) -> None:
        path = fixtures.make_text(self.tmp / "空文件.txt", "   \n\n")
        file_id = self.submit("空文件.txt", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_BLOCKED)
        self.upload_mock.assert_not_called()
        self.assertEqual(self.result_json(file_id)["lightrag"]["state"], "skipped")

    def test_garbled_text_is_blocked(self) -> None:
        path = fixtures.make_text(self.tmp / "乱码.txt", "\ufffd" * 400)
        file_id = self.submit("乱码.txt", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_BLOCKED)
        self.upload_mock.assert_not_called()

    def test_scanned_pdf_needs_ocr_without_network(self) -> None:
        path = fixtures.make_pdf(self.tmp / "扫描合同.pdf", ["", ""])
        file_id = self.submit("扫描合同.pdf", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_NEEDS_OCR)
        self.upload_mock.assert_not_called()
        self.ocr_session_mock.assert_not_called()  # OCR 关闭 → 零网络请求

    def test_unsupported_legacy_format_is_blocked(self) -> None:
        path = fixtures.make_text(self.tmp / "旧表格.xls", "legacy")
        file_id = self.submit("旧表格.xls", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_BLOCKED)
        self.assertIn("另存为", state["detail"])
        self.upload_mock.assert_not_called()

    def test_lightrag_failure_becomes_failed_state(self) -> None:
        self.open_gate()
        self.upload_mock.return_value = fixtures.FakeResponse(status_code=500, payload={"detail": "boom"})
        path = fixtures.make_text(self.tmp / "正常资料.md", "这是一份内容充足的资料。" * 10)
        file_id = self.submit("正常资料.md", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_FAILED)
        self.assertEqual(self.result_json(file_id)["lightrag"]["state"], "failed")


class IngestActionTests(IngestTestCase):
    def test_ocr_action_rejected_when_disabled(self) -> None:
        path = fixtures.make_pdf(self.tmp / "扫描件.pdf", [""])
        file_id = self.submit("扫描件.pdf", path)
        ingest.run_job(file_id)

        outcome = ingest.activate(file_id, ingest.ACTION_OCR)

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["http_status"], 409)
        self.assertIn("OCR 未启用", outcome["message"])

    def test_force_ingest_blocked_document(self) -> None:
        """重复行文档会被拦下，但用户仍可点「仍要入库」；走后台队列完成。"""
        path = fixtures.make_text(self.tmp / "重复.md", REPEATED_LINES)
        file_id = self.submit("重复.md", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_BLOCKED)

        outcome = ingest.activate(file_id, ingest.ACTION_FORCE)
        self.assertTrue(outcome["ok"])

        self.wait_for_state(file_id, ingest.STATE_DONE)  # 等后台工作线程处理完

        self.assertEqual(self.upload_mock.call_count, 1)
        result = self.result_json(file_id)
        self.assertEqual(result["state"], "done")
        self.assertTrue(result["quality"]["forced"])

    def test_force_rejected_without_text(self) -> None:
        path = fixtures.make_pdf(self.tmp / "空扫描.pdf", [""])
        file_id = self.submit("空扫描.pdf", path)
        ingest.run_job(file_id)

        outcome = ingest.activate(file_id, ingest.ACTION_FORCE)

        self.assertFalse(outcome["ok"])
        self.assertIn("没有可入库的文字", outcome["message"])
        self.upload_mock.assert_not_called()

    def test_unknown_action_and_file(self) -> None:
        self.assertEqual(ingest.activate("不存在", "ocr")["http_status"], 404)
        self.assertEqual(ingest.activate("不存在", "删除")["http_status"], 400)

    def test_status_flags_for_needs_ocr(self) -> None:
        path = fixtures.make_pdf(self.tmp / "扫描件.pdf", [""])
        file_id = self.submit("扫描件.pdf", path)
        ingest.run_job(file_id)

        payload = ingest.status(file_id)

        self.assertEqual(payload["state"], "needs_ocr")
        self.assertFalse(payload["can"]["ocr"])       # OCR 关闭 → 按钮不可用
        self.assertTrue(payload["can"]["retry"])
        self.assertFalse(payload["ocr_available"])
        self.assertIn("name", payload)

    def test_status_missing_file(self) -> None:
        self.assertIsNone(ingest.status("20260101000000-deadbeef"))

    def test_retry_after_failure(self) -> None:
        self.open_gate()
        self.upload_mock.side_effect = [
            fixtures.FakeResponse(status_code=500, payload={"detail": "boom"}),
            fixtures.FakeResponse(),
        ]
        path = fixtures.make_text(self.tmp / "正常资料.md", "内容充分的资料正文。" * 20)
        file_id = self.submit("正常资料.md", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_FAILED)

        ingest.run_job(file_id, ingest.ACTION_RETRY)

        self.assertEqual(self.result_json(file_id)["state"], "done")
        self.assertEqual(self.upload_mock.call_count, 2)

    def test_retry_after_done_is_rejected(self) -> None:
        """已入库的文件再点重试会造成重复文档 → 必须被拒绝。"""
        self.open_gate()
        path = fixtures.make_text(self.tmp / "已入库.md", "内容充分的资料正文。" * 20)
        file_id = self.submit("已入库.md", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_DONE)

        outcome = ingest.activate(file_id, ingest.ACTION_RETRY)

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["http_status"], 409)
        self.assertIn("已经入库", outcome["message"])
        self.assertEqual(self.upload_mock.call_count, 1)
        self.assertFalse(ingest.status(file_id)["can"]["retry"])

    def test_forced_ingest_is_audited(self) -> None:
        """人工放行必须留痕：forced / forced_at / forced_reasons。"""
        path = fixtures.make_text(self.tmp / "重复.md", REPEATED_LINES)
        file_id = self.submit("重复.md", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_BLOCKED)

        self.assertTrue(ingest.activate(file_id, ingest.ACTION_FORCE)["ok"])
        self.wait_for_state(file_id, ingest.STATE_DONE)

        quality = self.result_json(file_id)["quality"]
        self.assertTrue(quality["forced"])
        self.assertTrue(quality["forced_at"])
        self.assertTrue(quality["forced_reasons"])


class IngestImageOcrTests(IngestTestCase):
    """内嵌图片（Phase 9）：默认停在 needs_ocr；点「识别图片」后才送 OCR，随后复检入库。"""

    DOCX_BODY = [
        "2026 年第三季度工作总结",
        "本季度完成了资料库整理与归档工作，并补充历史资料 128 份；下一步计划接入语音问答"
        "与一键导出功能，同时继续优化检索效果与解析质量，确保扫描件与图片资料也能被正确识别。",
    ]

    def test_docx_with_images_stops_before_ocr(self) -> None:
        path = fixtures.make_docx_with_images(
            self.tmp / "带图总结.docx", self.DOCX_BODY, images=2
        )
        file_id = self.submit("带图总结.docx", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_NEEDS_OCR)
        self.upload_mock.assert_not_called()
        self.ocr_session_mock.assert_not_called()  # 未点按钮 → 零网络请求

        payload = ingest.status(file_id)
        self.assertEqual(payload["image_total"], 2)
        self.assertEqual(payload["unocr_images"], 2)
        self.assertFalse(payload["can"]["ocr"])        # OCR 关闭 → 按钮不可用
        self.assertTrue(payload["can"]["force"])        # 但可以只入库正文
        self.assertEqual(len(payload["assets"]), 2)

    def test_image_ocr_backfills_then_ingests(self) -> None:
        path = fixtures.make_docx_with_images(
            self.tmp / "带图总结.docx", self.DOCX_BODY, images=2
        )
        file_id = self.submit("带图总结.docx", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_NEEDS_OCR)

        # 图片回填 → 复检 → 入库这条链路本身；确认门见 tests/test_confirm.py
        self.enable_stub_ocr(ingest_confirm="off")
        self.assertTrue(ingest.status(file_id)["can"]["ocr"])
        outcome = ingest.activate(file_id, ingest.ACTION_OCR)
        self.assertTrue(outcome["ok"])
        self.wait_for_state(file_id, ingest.STATE_DONE)

        self.assertEqual(self.upload_mock.call_count, 1)
        _, payload, _ = self.upload_mock.call_args[0]
        text = payload.decode("utf-8")
        self.assertIn("图片 1", text)
        self.assertIn("离线替身 OCR", text)

        result = self.result_json(file_id)
        self.assertEqual(result["ocr"]["scope"], "images")
        self.assertEqual(result["ocr"]["recognized_images"], 2)
        self.assertTrue(all(item["ocr"]["done"] for item in result["assets"]))

    def test_force_after_image_stop_ingests_text_only(self) -> None:
        path = fixtures.make_docx_with_images(
            self.tmp / "带图方案.docx", self.DOCX_BODY, images=1
        )
        file_id = self.submit("带图方案.docx", path)
        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_NEEDS_OCR)

        self.assertTrue(ingest.activate(file_id, ingest.ACTION_FORCE)["ok"])
        self.wait_for_state(file_id, ingest.STATE_DONE)

        self.assertEqual(self.upload_mock.call_count, 1)
        self.ocr_session_mock.assert_not_called()
        result = self.result_json(file_id)
        self.assertFalse(result["assets"][0]["ocr"]["done"])
        self.assertTrue(result["quality"]["forced"])

    def test_tolerance_allows_small_number_of_images(self) -> None:
        """把 QUALITY_MAX_UNOCR_IMAGES 调大后，少量图片不再阻断入库。"""
        for patcher in fixtures.patch_settings(
            quality_max_unocr_images=5, ingest_confirm="off"
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        path = fixtures.make_docx_with_images(
            self.tmp / "带图周报.docx", self.DOCX_BODY, images=2
        )
        file_id = self.submit("带图周报.docx", path)

        state = ingest.run_job(file_id)

        self.assertEqual(state["state"], ingest.STATE_DONE)
        self.assertEqual(self.upload_mock.call_count, 1)

    def test_image_ocr_disabled_by_switch(self) -> None:
        """OCR_IMAGE_OCR=false：图片不参与判断，正文通过即可入库。"""
        for patcher in fixtures.patch_settings(ocr_image_ocr=False, ingest_confirm="off"):
            patcher.start()
            self.addCleanup(patcher.stop)

        path = fixtures.make_docx_with_images(
            self.tmp / "忽略图片.docx", self.DOCX_BODY, images=3
        )
        file_id = self.submit("忽略图片.docx", path)

        self.assertEqual(ingest.run_job(file_id)["state"], ingest.STATE_DONE)


if __name__ == "__main__":
    unittest.main()

