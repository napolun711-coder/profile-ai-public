"""本地解析单测（Phase 8）：验证各格式都能产出统一 Markdown，且失败可解释。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core.config import Settings
from app.services import parse as parse_service
from tests import fixtures


class ParseRouteTests(unittest.TestCase):
    def test_detect_route(self) -> None:
        cases = {
            "报告.PDF": "pdf",
            "笔记.md": "text",
            "表格.xlsx": "xlsx",
            "文档.docx": "docx",
            "讲稿.pptx": "pptx",
            "扫描件.jpg": "image",
            "老旧.doc": "unsupported",
            "未知.bin": "unsupported",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_service.detect_route(name), expected)


class ParseTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_markdown_utf8(self) -> None:
        path = fixtures.make_text(self.tmp / "简历.md", "# 个人简历\n\n姓名：张伟\n从事数据分析工作。")
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "text")
        self.assertIn("张伟", result.text)
        self.assertGreater(result.char_total, 10)

    def test_gbk_fallback(self) -> None:
        path = fixtures.make_text(self.tmp / "旧笔记.txt", "这是一段 GBK 编码的中文内容。", encoding="gbk")
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertIn("GBK 编码", result.text)

    def test_html_strip(self) -> None:
        html = "<html><head><style>b{color:red}</style></head><body><p>第一段</p><p>第二段</p></body></html>"
        path = fixtures.make_text(self.tmp / "页面.html", html)
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertIn("第一段", result.text)
        self.assertIn("第二段", result.text)
        self.assertNotIn("<p>", result.text)
        self.assertNotIn("color:red", result.text)

    def test_invalid_json_kept_as_text(self) -> None:
        path = fixtures.make_text(self.tmp / "坏数据.json", "{不是合法 JSON")
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertIn("不是合法 JSON", result.text)

    def test_truncate_by_limit(self) -> None:
        path = fixtures.make_text(self.tmp / "长文.txt", "内容" * 200)
        limit = 50
        with mock.patch.object(
            parse_service, "get_settings", return_value=Settings(parse_max_chars=limit)
        ):
            result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), limit)

    def test_empty_file(self) -> None:
        path = fixtures.make_text(self.tmp / "空.txt", "")
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.char_total, 0)


class ParseBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pdf_with_text_layer(self) -> None:
        path = fixtures.make_pdf(
            self.tmp / "报告.pdf",
            ["Annual budget review 2026 with details", "Second page summary of the project"],
        )
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "pdf")
        self.assertEqual(result.pages, 2)
        self.assertIn("Annual budget review", result.text)
        self.assertIn("Second page summary", result.text)
        self.assertGreater(result.char_total, 50)

    def test_pdf_without_text_layer(self) -> None:
        """扫描件样式：页数正常但抽不出文字（后续由质量门判为 needs_ocr）。"""
        path = fixtures.make_pdf(self.tmp / "扫描件.pdf", ["", ""])
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.pages, 2)
        self.assertEqual(result.char_total, 0)

    def test_docx_paragraph_and_table(self) -> None:
        path = fixtures.make_docx(
            self.tmp / "总结.docx",
            ["2026 年第三季度工作总结", "本季度完成了资料库整理与归档。"],
            table=[["科目", "金额"], ["差旅费", "1200"]],
        )
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "docx")
        self.assertIn("第三季度工作总结", result.text)
        self.assertIn("| 科目 | 金额 |", result.text)
        self.assertIn("差旅费", result.text)

    def test_xlsx_sheets_and_rows(self) -> None:
        path = fixtures.make_xlsx(
            self.tmp / "预算.xlsx",
            "预算表",
            [["项目", "金额"], ["房租", 2000], ["餐饮", 1500]],
        )
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "xlsx")
        self.assertIn("## 预算表", result.text)
        self.assertIn("| 房租 | 2000 |", result.text)

    def test_pptx_slide_text(self) -> None:
        path = fixtures.make_pptx(self.tmp / "演示.pptx", ["封面：年度计划", "第二页：执行步骤"])
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "pptx")
        self.assertIn("封面：年度计划", result.text)
        self.assertIn("第二页：执行步骤", result.text)

    def test_image_route_needs_ocr(self) -> None:
        path = fixtures.make_png(self.tmp / "扫描件.png")
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "image")
        self.assertEqual(result.text, "")
        self.assertIn("OCR", result.detail)


class ParseAssetTests(unittest.TestCase):
    """Phase 9：内嵌图片抽取（只取字节，不联网、不识别）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_docx_images_are_extracted_in_order(self) -> None:
        path = fixtures.make_docx_with_images(
            self.tmp / "带图总结.docx", ["2026 年第三季度工作总结"], images=2
        )
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "docx")
        self.assertEqual(result.images, 2)
        self.assertEqual(len(result.assets), 2)
        self.assertEqual(result.assets[0].data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("word/media", result.assets[0].source)
        self.assertIn("未识别", result.detail)
        self.assertEqual([asset.order for asset in result.assets], [0, 1])
        self.assertGreater(result.asset_bytes, 0)

    def test_pptx_images_are_extracted_by_slide(self) -> None:
        path = fixtures.make_pptx(self.tmp / "带图演示.pptx", ["封面：年度计划"], images=1)
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.route, "pptx")
        self.assertEqual(len(result.assets), 1)
        self.assertIn("幻灯片 1", result.assets[0].source)

    def test_xlsx_images_are_extracted(self) -> None:
        path = fixtures.make_xlsx(self.tmp / "表格.xlsx", "预算表", [["项目", "金额"], ["房租", 2000]])
        result = parse_service.parse_file(path)

        self.assertTrue(result.ok)
        self.assertEqual(result.assets, [])  # 普通表格没有内嵌图片

    def test_asset_limit_is_respected(self) -> None:
        path = fixtures.make_docx_with_images(
            self.tmp / "多图.docx", ["说明文档正文"], images=3
        )
        settings = Settings(parse_max_assets=2)
        with mock.patch.object(parse_service, "get_settings", return_value=settings):
            result = parse_service.parse_file(path)

        self.assertEqual(len(result.assets), 2)
        self.assertIn("上限", result.detail)

    def test_no_images_means_empty_assets(self) -> None:
        path = fixtures.make_docx(self.tmp / "纯文字.docx", ["只有正文，没有图片。"])
        result = parse_service.parse_file(path)

        self.assertEqual(result.assets, [])
        self.assertEqual(result.images, 0)

    def test_unsupported_legacy_format(self) -> None:
        path = fixtures.make_text(self.tmp / "旧文档.doc", "legacy word")
        result = parse_service.parse_file(path)

        self.assertFalse(result.ok)
        self.assertEqual(result.route, "unsupported")
        self.assertIn("另存为", result.detail)

    def test_broken_pdf_is_reported(self) -> None:
        path = fixtures.make_text(self.tmp / "损坏.pdf", "这不是一个真正的 PDF")
        result = parse_service.parse_file(path)

        self.assertFalse(result.ok)
        self.assertIn("解析失败", result.detail)


if __name__ == "__main__":
    unittest.main()

