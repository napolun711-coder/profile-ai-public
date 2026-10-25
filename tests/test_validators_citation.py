"""P1D：CitationValidator 的纯函数用例（无网络、无磁盘）。"""

from __future__ import annotations

import json
import unittest

from app.services.context_builder import Citation
from app.services.validators import CitationValidator, Issue, ValidationReport
from app.services.validators.base import Validator


def entry(
    reference_id: str = "1",
    *,
    label: str = "",
    citation_id: str = "",
    kind: str = "chunk",
) -> dict:
    return {
        "citation_id": citation_id or f"c-{reference_id}",
        "label": label or f"[{reference_id}]",
        "kind": kind,
        "reference_id": reference_id,
    }


def citations(*ids: str) -> dict:
    return {f"c{value}": entry(value) for value in ids}


class FormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = CitationValidator()
        self.context = citations("1", "2", "3", "4", "5", "6", "7")

    def test_supported_formats(self) -> None:
        report = self.validator.validate(
            {"answer": "[1] 【2】 [3,4] [5-7]", "citation_map": self.context}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.stats["cited_ids"], ["1", "2", "3", "4", "5", "6", "7"])
        self.assertEqual(report.stats["markers"], 4)
        self.assertEqual(report.stats["formats"], {"bracket": 3, "corner": 1})

    def test_leading_zero_is_normalized(self) -> None:
        report = self.validator.validate(
            {"answer": "[01]", "citation_map": citations("1")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["cited_ids"], ["1"])

    def test_markdown_links_and_plain_brackets_are_ignored(self) -> None:
        report = self.validator.validate(
            {
                "answer": "参见[链接](https://example.com)和[说明]。",
                "citation_map": citations("1"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["markers"], 0)
        self.assertEqual([issue.code for issue in report.issues], ["missing_citation"])

    def test_non_numeric_citation_is_counted_but_not_matched(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[1]",
                "citation_map": [{"citation_id": "img", "label": "[img-1]"}],
            }
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.stats["non_numeric_citation_count"], 1)
        self.assertEqual(report.stats["numeric_citation_count"], 0)
        self.assertEqual(
            [issue.code for issue in report.issues], ["unknown_citation"]
        )


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = CitationValidator()

    def test_unknown_citation_is_error(self) -> None:
        report = self.validator.validate(
            {"answer": "[2]", "citation_map": citations("1")}
        )

        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].code, "unknown_citation")
        self.assertEqual(report.errors[0].ref, "2")
        self.assertEqual(report.stats["unknown_ids"], ["2"])

    def test_missing_citation_is_warning(self) -> None:
        report = self.validator.validate(
            {"answer": "没有标记", "citation_map": citations("1")}
        )

        self.assertTrue(report.ok)
        self.assertEqual([issue.code for issue in report.warnings], ["missing_citation"])

    def test_duplicate_citation_is_warning(self) -> None:
        report = self.validator.validate(
            {"answer": "[1] 和 [1]", "citation_map": citations("1")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(
            [issue.code for issue in report.warnings], ["duplicate_citation"]
        )
        self.assertEqual(report.stats["duplicate_ids"], ["1"])

    def test_empty_answer_is_error(self) -> None:
        report = self.validator.validate(
            {"answer": "  \n", "citation_map": citations("1")}
        )

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["empty_answer"])
        self.assertFalse(report.stats["answer_nonempty"])

    def test_empty_context_with_markers_is_dangling(self) -> None:
        report = self.validator.validate({"answer": "[1]", "citation_map": {}})

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["dangling_citation"])
        self.assertEqual(report.stats["citation_count"], 0)

    def test_context_with_only_non_numeric_labels_is_not_dangling(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[1]",
                "citation_map": [{"citation_id": "img", "label": "[img-1]"}],
            }
        )

        self.assertNotIn("dangling_citation", [issue.code for issue in report.issues])
        self.assertIn("unknown_citation", [issue.code for issue in report.issues])

    def test_inverted_range_is_warning_and_not_expanded(self) -> None:
        report = self.validator.validate(
            {"answer": "[3-1]", "citation_map": citations("1", "2")}
        )

        self.assertTrue(report.ok)
        codes = [issue.code for issue in report.issues]
        self.assertIn("invalid_citation", codes)
        self.assertIn("missing_citation", codes)
        self.assertEqual(report.stats["cited_ids"], [])

    def test_range_over_limit_is_warning(self) -> None:
        report = self.validator.validate(
            {"answer": "[1-101]", "citation_map": citations("1", "2")}
        )

        self.assertTrue(report.ok)
        self.assertIn("invalid_citation", [issue.code for issue in report.issues])
        self.assertEqual(report.stats["cited_ids"], [])


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = CitationValidator()

    def test_citations_alias_key_is_supported(self) -> None:
        report = self.validator.validate(
            {"answer": "[1]", "citations": citations("1")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["cited_ids"], ["1"])

    def test_actual_citation_objects_are_supported(self) -> None:
        context = [
            Citation(citation_id="c1", label="[1]", kind="chunk", reference_id="1")
        ]
        report = self.validator.validate(
            {"answer": "[1]", "citation_map": context}
        )

        self.assertTrue(report.ok)

    def test_numeric_citation_id_is_supported(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[7]",
                "citation_map": [{"citation_id": "7", "label": "[chunk:7]"}],
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["numeric_citation_count"], 1)

    def test_invalid_payload_does_not_raise(self) -> None:
        report = self.validator.validate([])  # type: ignore[arg-type]

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["invalid_input"])

    def test_invalid_answer_type_does_not_raise(self) -> None:
        report = self.validator.validate({"answer": 1})

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["invalid_input"])
        self.assertFalse(report.stats["answer_nonempty"])

    def test_invalid_citations_type_does_not_raise(self) -> None:
        report = self.validator.validate(
            {"answer": "[1]", "citation_map": "not-a-map"}
        )

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["invalid_input"])

    def test_report_is_deterministic_and_contains_no_answer_body(self) -> None:
        secret = "这是答案正文，不能进报告"
        payload = {
            "answer": f"{secret} [1] [9]",
            "citation_map": citations("1"),
        }

        first = self.validator.validate(payload)
        second = self.validator.validate(payload)
        rendered = json.dumps(first.to_dict(), ensure_ascii=False)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertNotIn(secret, rendered)


class ContractTests(unittest.TestCase):
    def test_issue_rejects_unknown_severity(self) -> None:
        with self.assertRaises(ValueError):
            Issue("x", "消息", "fatal")

    def test_report_ok_only_depends_on_errors(self) -> None:
        report = ValidationReport("citation", "1")
        report.add(Issue("missing_citation", "缺少引用", "warning"))
        self.assertTrue(report.ok)
        report.add(Issue("unknown_citation", "未知引用", "error"))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(len(report.warnings), 1)

    def test_validator_protocol_shape(self) -> None:
        validator = CitationValidator()

        self.assertIsInstance(validator, Validator)
        self.assertEqual(validator.name, "citation")
        self.assertEqual(validator.version, "1")


if __name__ == "__main__":
    unittest.main()
