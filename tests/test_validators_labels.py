"""P2D：LabelCitationValidator 的纯函数用例（无网络、无磁盘）。"""

from __future__ import annotations

import json
import unittest

from app.services.context_builder import Citation
from app.services.validators import (
    DEFAULT_LABEL_PREFIXES,
    LabelCitationValidator,
    ValidationReport,
)
from app.services.validators.base import Validator


def entry(label: str, *, citation_id: str = "", kind: str = "image") -> dict:
    return {
        "citation_id": citation_id or f"c-{label}",
        "label": label,
        "kind": kind,
    }


def citations(*labels: str) -> dict:
    return {f"c{index}": entry(label) for index, label in enumerate(labels)}


class FormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = LabelCitationValidator()

    def test_supported_formats(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[img-1] 【img-2】 [img-3,img-4] [doc-1]",
                "citation_map": citations(
                    "[img-1]", "[img-2]", "[img-3]", "[img-4]", "[doc-1]"
                ),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.issues, [])
        self.assertEqual(
            report.stats["cited_ids"], ["doc-1", "img-1", "img-2", "img-3", "img-4"]
        )
        self.assertEqual(report.stats["markers"], 4)
        self.assertEqual(report.stats["formats"], {"bracket": 3, "corner": 1})

    def test_leading_zero_is_normalized(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-01]", "citation_map": citations("[img-1]")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["cited_ids"], ["img-1"])

    def test_prefix_case_is_normalized(self) -> None:
        report = self.validator.validate(
            {"answer": "[IMG-1]", "citation_map": citations("[img-1]")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["cited_ids"], ["img-1"])

    def test_markdown_links_and_plain_brackets_are_ignored(self) -> None:
        report = self.validator.validate(
            {
                "answer": "参见[链接](https://example.com)和[说明]。",
                "citation_map": citations("[img-1]"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["markers"], 0)
        self.assertEqual([issue.code for issue in report.issues], ["missing_citation"])

    def test_markdown_image_embed_is_ignored(self) -> None:
        report = self.validator.validate(
            {
                "answer": "![img-1](https://example.com/a.png)",
                "citation_map": citations("[img-1]"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["markers"], 0)
        self.assertEqual([issue.code for issue in report.issues], ["missing_citation"])

    def test_chinese_parenthetical_after_label_is_not_a_link(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[img-1](见上图)",
                "citation_map": citations("[img-1]"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["markers"], 1)
        self.assertEqual(report.stats["cited_ids"], ["img-1"])

    def test_unknown_prefix_group_is_treated_as_plain_text(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[foo-1] 和 [img-1,foo-2]",
                "citation_map": citations("[img-1]"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["markers"], 0)
        self.assertEqual([issue.code for issue in report.issues], ["missing_citation"])

    def test_declared_prefix_becomes_known(self) -> None:
        report = self.validator.validate(
            {"answer": "[figure-9]", "citation_map": citations("[figure-1]")}
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.stats["unknown_ids"], ["figure-9"])
        self.assertEqual([issue.code for issue in report.errors], ["unknown_citation"])


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = LabelCitationValidator()

    def test_default_prefix_unknown_number_is_error(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-9]", "citation_map": citations("[img-1]")}
        )

        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].code, "unknown_citation")
        self.assertEqual(report.errors[0].ref, "img-9")
        self.assertEqual(report.stats["unknown_ids"], ["img-9"])

    def test_missing_citation_is_warning(self) -> None:
        report = self.validator.validate(
            {"answer": "没有标记", "citation_map": citations("[img-1]")}
        )

        self.assertTrue(report.ok)
        self.assertEqual([issue.code for issue in report.warnings], ["missing_citation"])

    def test_duplicate_citation_is_warning(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-1] 和 [img-1]", "citation_map": citations("[img-1]")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(
            [issue.code for issue in report.warnings], ["duplicate_citation"]
        )
        self.assertEqual(report.stats["duplicate_ids"], ["img-1"])

    def test_empty_answer_is_error(self) -> None:
        report = self.validator.validate(
            {"answer": "  \n", "citation_map": citations("[img-1]")}
        )

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["empty_answer"])
        self.assertFalse(report.stats["answer_nonempty"])

    def test_empty_context_with_labels_is_dangling(self) -> None:
        report = self.validator.validate({"answer": "[img-1]", "citation_map": {}})

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["dangling_citation"])
        self.assertEqual(report.stats["citation_count"], 0)

    def test_numeric_only_context_with_label_marker_is_dangling(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-1]", "citation_map": citations("[1]")}
        )

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["dangling_citation"])
        self.assertEqual(report.stats["label_citation_count"], 0)
        self.assertEqual(report.stats["non_label_citation_count"], 1)

    def test_empty_context_without_labels_is_ok(self) -> None:
        report = self.validator.validate({"answer": "没有引用", "citation_map": {}})

        self.assertTrue(report.ok)
        self.assertEqual(report.issues, [])

    def test_mixed_label_and_numeric_is_counted(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[img-1]",
                "citation_map": citations("[img-1]", "[1]"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["citation_count"], 2)
        self.assertEqual(report.stats["label_citation_count"], 1)
        self.assertEqual(report.stats["non_label_citation_count"], 1)


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = LabelCitationValidator()

    def test_citations_alias_key_is_supported(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-1]", "citations": citations("[img-1]")}
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["cited_ids"], ["img-1"])

    def test_actual_citation_objects_are_supported(self) -> None:
        context = [
            Citation(citation_id="c1", label="[img-1]", kind="image")
        ]
        report = self.validator.validate(
            {"answer": "[img-1]", "citation_map": context}
        )

        self.assertTrue(report.ok)

    def test_bare_label_strings_are_supported(self) -> None:
        report = self.validator.validate(
            {"answer": "[img-1]", "citation_map": ["img-1"]}
        )

        self.assertTrue(report.ok)

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
            {"answer": "[img-1]", "citation_map": "not-a-map"}
        )

        self.assertFalse(report.ok)
        self.assertEqual([issue.code for issue in report.issues], ["invalid_input"])

    def test_report_is_deterministic_and_contains_no_answer_body(self) -> None:
        secret = "这是答案正文，不能进报告"
        payload = {
            "answer": f"{secret} [img-1] [img-9]",
            "citation_map": citations("[img-1]"),
        }

        first = self.validator.validate(payload)
        second = self.validator.validate(payload)
        rendered = json.dumps(first.to_dict(), ensure_ascii=False)

        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertNotIn(secret, rendered)

    def test_single_citation_mapping_is_supported(self) -> None:
        report = self.validator.validate(
            {
                "answer": "[img-1]",
                "citation_map": entry("[img-1]", citation_id="c1"),
            }
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.stats["citation_count"], 1)


class ContractTests(unittest.TestCase):
    def test_validator_protocol_shape(self) -> None:
        validator = LabelCitationValidator()

        self.assertIsInstance(validator, Validator)
        self.assertEqual(validator.name, "citation_labels")
        self.assertEqual(validator.version, "1")

    def test_default_prefixes_are_exposed(self) -> None:
        self.assertIn("img", DEFAULT_LABEL_PREFIXES)
        self.assertIn("doc", DEFAULT_LABEL_PREFIXES)

    def test_report_ok_only_depends_on_errors(self) -> None:
        report = ValidationReport("citation_labels", "1")
        self.assertTrue(report.ok)
        self.assertIsInstance(report, ValidationReport)


if __name__ == "__main__":
    unittest.main()
