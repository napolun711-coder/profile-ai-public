"""P3C.2：回答内容校验器离线单测（确定性、不联网）。"""

from __future__ import annotations

import json
import unittest

from app.services.context_builder import Citation
from app.services.validators.content import (
    ContentValidator,
    error_codes,
    failure_kind,
    failure_message,
    is_refusal,
    parse_citation_labels,
    validate_content,
    warning_codes,
)


def citations() -> dict:
    return {
        "chunk:c1": Citation(
            citation_id="chunk:c1",
            label="[1]",
            kind="chunk",
            reference_id="1",
            doc_name="a.md",
        ),
        "image:img1": Citation(
            citation_id="image:img1",
            label="[img-1]",
            kind="image",
            doc_name="照片.md",
            file_id="f1",
            image_id="img:f1:0",
        ),
    }


def validate(
    answer: str,
    *,
    evidence=(),
    done_reason: str = "",
    refusal=None,
    citation_map=None,
):
    return validate_content(
        answer=answer,
        citations=citations() if citation_map is None else citation_map,
        evidence_texts=list(evidence),
        done_reason=done_reason,
        refusal=refusal,
    )


class ParseTests(unittest.TestCase):
    def test_lists_ranges_and_named_labels(self) -> None:
        self.assertEqual(
            parse_citation_labels("见 [1,2]、[1-3]、【img-1】 与 [chunk:abc]。"),
            ["[1]", "[2]", "[3]", "[img-1]", "[chunk:abc]"],
        )

    def test_ignores_normal_brackets_and_links(self) -> None:
        self.assertEqual(
            parse_citation_labels("这是 [图片] 和 [链接](x)，不是引用。"),
            [],
        )

    def test_leading_zero_named_label_is_normalized(self) -> None:
        self.assertEqual(parse_citation_labels("见 [img-01]"), ["[img-1]"])

    def test_refusal_markers(self) -> None:
        self.assertTrue(is_refusal("资料中没有足够信息。"))
        self.assertFalse(is_refusal("结论 [1]。"))


class RuleTests(unittest.TestCase):
    def test_empty_answer_is_hard_failure(self) -> None:
        report = validate("   ")

        self.assertFalse(report.ok)
        self.assertEqual(error_codes(report), ["empty_answer"])
        self.assertEqual(failure_kind(report), "local_empty")

    def test_valid_answer_passes(self) -> None:
        report = validate("结论 [1]。")

        self.assertTrue(report.ok)
        self.assertEqual(error_codes(report), [])
        self.assertIn("too_short", warning_codes(report))

    def test_unknown_numeric_and_named_labels(self) -> None:
        report = validate("结论 [1] 与 [9] 和 [img-9]。")

        self.assertFalse(report.ok)
        self.assertEqual(error_codes(report), ["unknown_citation"])
        self.assertIn("[9]", report.stats["unknown_labels"])
        self.assertIn("[img-9]", report.stats["unknown_labels"])
        self.assertEqual(failure_kind(report), "local_unverified")

    def test_no_citation_is_hard_failure_unless_refusal(self) -> None:
        report = validate("一段没有引用的总结。")

        self.assertFalse(report.ok)
        self.assertEqual(error_codes(report), ["no_citation"])

        refusal = validate("资料中没有足够信息。")
        self.assertTrue(refusal.ok)
        self.assertEqual(refusal.stats["refusal"], True)
        self.assertEqual(warning_codes(refusal), ["refusal_with_evidence"])

    def test_truncation_uses_done_reason(self) -> None:
        report = validate("结论 [1]。", done_reason="length")

        self.assertFalse(report.ok)
        self.assertEqual(error_codes(report), ["truncated"])
        self.assertEqual(failure_kind(report), "local_truncated")

        self.assertTrue(validate("结论 [1]。", done_reason="stop").ok)

    def test_repetition_loop_is_hard_failure(self) -> None:
        report = validate("同一句话反复出现。" * 80 + " [1]")

        self.assertFalse(report.ok)
        self.assertIn("repetition", error_codes(report))
        self.assertEqual(failure_kind(report), "local_repetitive")

    def test_garbled_text_is_hard_failure(self) -> None:
        report = validate("正常内容 [1]" + "�" * 3)

        self.assertFalse(report.ok)
        self.assertIn("garbled", error_codes(report))
        self.assertEqual(failure_kind(report), "local_garbled")

    def test_low_citation_coverage_is_warning(self) -> None:
        answer = (
            "项目甲在 2024 年启动 [1]。"
            "它包含三个子模块。"
            "每个模块都有独立负责人。"
            "整体进度符合预期。"
        )
        report = validate(answer)

        self.assertTrue(report.ok)
        self.assertIn("low_citation_coverage", warning_codes(report))

    def test_unsupported_number_is_warning_only_with_evidence(self) -> None:
        answer = "项目在 2024 年启动 [1]，共 128 人。"

        with_evidence = validate(answer, evidence=["项目在 2024 年启动。"])
        self.assertTrue(with_evidence.ok)
        self.assertIn("unsupported_number", warning_codes(with_evidence))
        self.assertIn("128", with_evidence.stats["unsupported_numbers"])

        without_evidence = validate(answer)
        self.assertNotIn("unsupported_number", warning_codes(without_evidence))

    def test_supported_number_is_not_flagged(self) -> None:
        report = validate(
            "项目在 2024 年启动 [1]，共 128 人。",
            evidence=["项目在 2024 年启动，共 128 人。"],
        )

        self.assertTrue(report.ok)
        self.assertNotIn("unsupported_number", warning_codes(report))

    def test_report_never_copies_answer_text(self) -> None:
        secret = "这段回答包含机密内容"
        report = validate(secret + " 128 [9]。", evidence=["无关资料"])
        rendered = json.dumps(report.to_dict(), ensure_ascii=False)

        self.assertNotIn(secret, rendered)
        self.assertNotIn("无关资料", rendered)
        self.assertNotIn(secret, failure_message(report))

    def test_invalid_payload(self) -> None:
        report = ContentValidator().validate("not-a-mapping")

        self.assertFalse(report.ok)
        self.assertEqual(error_codes(report), ["invalid_input"])


if __name__ == "__main__":
    unittest.main()
