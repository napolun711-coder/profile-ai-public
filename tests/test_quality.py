"""质量门单测（Phase 8）：阈值判定必须稳定、可解释。

Phase 18 起质检是**硬门**（没有总开关），所以这里不再有"总开关关掉 → 直接放行"的用例；
被忽略的旧开关由 ``tests/test_quality_hardgate.py`` 单独守住。
"""

from __future__ import annotations

import dataclasses
import unittest

from app.services import quality as q
from tests import fixtures

OPTIONS = q.QualityOptions()

CHINESE_DOC = (
    "本地知识库助手使用说明\n\n"
    "第一步：把资料上传到本地知识库，系统会先做本地解析与质量检测。\n"
    "第二步：只有通过检测的文本才会进入知识库，避免扫描件与乱码污染问答结果。\n"
    "第三步：如果文档是扫描件或图片，可以在网页上点击「OCR 解析」调用云端识别。\n"
    "第四步：确认入库后即可用自然语言提问，例如「帮我总结季度预算变化」。\n"
)

ENGLISH_DOC = (
    "Quarterly review summary\n\n"
    "The local parser extracts text from PDF, Word, Excel and PowerPoint files. "
    "Every document passes a quality gate before it is sent to the knowledge base. "
    "Scanned documents are detected by measuring characters per page.\n"
)


class MetricsTests(unittest.TestCase):
    def test_metrics_of_chinese_text(self) -> None:
        metrics = q.compute_metrics(CHINESE_DOC, pages=1)

        self.assertEqual(metrics["char_total"], sum(1 for ch in CHINESE_DOC if not ch.isspace()))
        self.assertGreater(metrics["readable_ratio"], 0.85)
        self.assertEqual(metrics["garbled_ratio"], 0.0)
        self.assertEqual(metrics["chars_per_page"], metrics["char_total"])

    def test_repeat_ratio(self) -> None:
        text = "\n".join(["重复内容"] * 10)
        metrics = q.compute_metrics(text, pages=1)

        self.assertAlmostEqual(metrics["repeat_line_ratio"], 0.9, places=2)

    def test_chars_per_page(self) -> None:
        metrics = q.compute_metrics("一二三四五六七八九十" * 5, pages=5)

        self.assertEqual(metrics["char_total"], 50)
        self.assertEqual(metrics["chars_per_page"], 10.0)


class DecisionTests(unittest.TestCase):
    def test_normal_chinese_document_passes(self) -> None:
        report = q.evaluate(CHINESE_DOC, route="text", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_PASS)
        self.assertGreaterEqual(report.score, OPTIONS.pass_score)
        self.assertEqual(report.reasons, [])

    def test_normal_english_document_passes(self) -> None:
        """英文资料不应被"中文占比"误杀。"""
        report = q.evaluate(ENGLISH_DOC, route="pdf", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_PASS)

    def test_empty_text_file_is_blocked(self) -> None:
        report = q.evaluate("", route="text", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_BLOCKED)
        self.assertIn("没有可提取的文字", report.reasons[0])

    def test_empty_pdf_needs_ocr(self) -> None:
        report = q.evaluate("", route="pdf", pages=3, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_NEEDS_OCR)
        self.assertIn("OCR", report.reasons[0])

    def test_image_needs_ocr(self) -> None:
        report = q.evaluate("", route="image", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_NEEDS_OCR)

    def test_scan_like_pdf_needs_ocr(self) -> None:
        """每页只有几十个字符 → 疑似扫描件（文字层残缺）。"""
        report = q.evaluate("残缺文字" * 40, route="pdf", pages=10, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_NEEDS_OCR)
        self.assertIn("每页", report.reasons[0])

    def test_short_text_is_blocked(self) -> None:
        report = q.evaluate("太短了", route="text", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_BLOCKED)
        self.assertTrue(any("过少" in reason for reason in report.reasons))

    def test_garbled_text_is_blocked(self) -> None:
        garbled = "\ufffd\ufffd\ufffd" * 100
        report = q.evaluate(garbled, route="text", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_BLOCKED)
        self.assertTrue(
            any("可读字符" in reason or "乱码" in reason for reason in report.reasons)
        )

    def test_repeated_lines_are_blocked(self) -> None:
        report = q.evaluate("\n".join(["同样的一行"] * 50), route="text", pages=1, options=OPTIONS)

        self.assertEqual(report.level, q.LEVEL_BLOCKED)
        self.assertTrue(any("重复行" in reason for reason in report.reasons))

    def test_gate_has_no_master_switch(self) -> None:
        """Phase 18：质检是硬门 —— 数据结构里根本没有「关掉它」的字段。"""
        options = q.QualityOptions()

        self.assertFalse(hasattr(options, "enabled"))
        self.assertEqual([item.name for item in dataclasses.fields(options) if item.name == "enabled"], [])

    def test_score_is_within_range(self) -> None:
        for text, route, pages in (
            (CHINESE_DOC, "text", 1),
            ("", "pdf", 5),
            ("\ufffd" * 500, "text", 1),
        ):
            with self.subTest(route=route, pages=pages):
                report = q.evaluate(text, route=route, pages=pages, options=OPTIONS)
                self.assertGreaterEqual(report.score, 0)
                self.assertLessEqual(report.score, 100)

    def test_describe_is_readable(self) -> None:
        report = q.evaluate("", route="text", pages=1, options=OPTIONS)

        self.assertIn("未通过质检", q.describe(report))


class ProfileTests(unittest.TestCase):
    """Phase 9：质量门预设 + OCR 路由可配。"""

    def test_standard_profile_matches_phase8_defaults(self) -> None:
        options = q.options_from_settings(
            fixtures.settings_with(quality_profile="standard", quality_min_chars=999)
        )

        # 预设优先：即使自定义了数值，standard 仍然使用内置阈值
        self.assertEqual(options.min_chars, q.QualityOptions().min_chars)
        self.assertEqual(options.pass_score, q.QualityOptions().pass_score)

    def test_custom_profile_uses_individual_values(self) -> None:
        options = q.options_from_settings(
            fixtures.settings_with(quality_profile="custom", quality_min_chars=120)
        )

        self.assertEqual(options.min_chars, 120)

    def test_strict_is_stricter_than_standard(self) -> None:
        standard = q.options_from_settings(fixtures.settings_with(quality_profile="standard"))
        strict = q.options_from_settings(fixtures.settings_with(quality_profile="strict"))

        self.assertGreater(strict.min_chars, standard.min_chars)
        self.assertGreater(strict.min_readable_ratio, standard.min_readable_ratio)
        self.assertLess(strict.max_garbled_ratio, standard.max_garbled_ratio)
        self.assertGreater(strict.pass_score, standard.pass_score)

    def test_loose_profile_accepts_short_text(self) -> None:
        text = "这是一段比较短的资料文字，但内容本身是可读的，适合宽松阈值。"
        loose = q.options_from_settings(fixtures.settings_with(quality_profile="loose"))
        standard = q.options_from_settings(fixtures.settings_with(quality_profile="standard"))

        self.assertEqual(q.evaluate(text, route="text", pages=1, options=loose).level, q.LEVEL_PASS)
        self.assertEqual(
            q.evaluate(text, route="text", pages=1, options=standard).level, q.LEVEL_BLOCKED
        )

    def test_master_switch_is_ignored(self) -> None:
        """Phase 18：旧 .env 里的 QUALITY_ENABLED=false 会被忽略 —— 阈值与结论完全不变。"""
        options = q.options_from_settings(
            fixtures.settings_with(quality_profile="strict", quality_enabled=False)
        )
        report = q.evaluate("", route="text", pages=1, options=options)

        # 空文本在 strict 下必然被拦（不再是"总开关关掉 → 直接 pass"）
        self.assertEqual(options.min_chars, q.PROFILES["strict"].min_chars)
        self.assertEqual(report.level, q.LEVEL_BLOCKED)
        self.assertFalse(any("已关闭" in reason for reason in report.reasons))

    def test_ocr_routes_are_configurable(self) -> None:
        """把 docx 加入 QUALITY_OCR_ROUTES 后，短 docx 会被判为需要 OCR。"""
        default = q.evaluate("", route="docx", pages=1, options=OPTIONS)
        widened = q.evaluate("", route="docx", pages=1, options=OPTIONS, routes=("pdf", "image", "docx"))

        self.assertEqual(default.level, q.LEVEL_BLOCKED)
        self.assertEqual(widened.level, q.LEVEL_NEEDS_OCR)

    def test_ocr_routes_helper_reads_settings(self) -> None:
        settings = fixtures.settings_with(quality_ocr_routes=("pdf", "docx"))

        self.assertEqual(q.ocr_routes(settings), ("pdf", "docx"))
        self.assertEqual(q.ocr_routes(fixtures.settings_with()), ("pdf", "image"))


if __name__ == "__main__":
    unittest.main()
