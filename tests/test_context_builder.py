"""P1C：Context Builder v1 的纯函数用例（无网络、无磁盘）。

重点钉住：
1. 确定性：相同输入连续两次构建完全相等；
2. 引用编号：`reference_id` 原样继承，同一文档的多个 chunk 共享编号且**不丢正文**；
3. 预算：`per_item_chars` / `max_items` / `total` 三道闸都生效，丢弃项不记录正文；
4. gallery 关联只补充已召回的证据，不新增、不修改输入、lookup 失败即降级。
"""

from __future__ import annotations

import json
import unittest

from app.plugins.adapters.knowledge import evidence_from_query_data
from app.plugins.contracts import Evidence
from app.services.context_builder import (
    CONTEXT_VERSION,
    Citation,
    RuleSet,
    TokenBudget,
    build_context,
    enrich_with_gallery,
    estimate_tokens,
)


def ev(
    kind: str = "chunk",
    *,
    evidence_id: str = "",
    source_id: str = "",
    doc_name: str = "",
    text: str = "",
    image_id: str = "",
    file_id: str = "",
    metadata: dict | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id or f"{kind}:{source_id or 'x'}",
        kind=kind,
        source_id=source_id,
        doc_name=doc_name,
        text=text,
        image_id=image_id,
        file_id=file_id,
        metadata=dict(metadata or {}),
    )


class DeterminismTests(unittest.TestCase):
    def test_same_input_gives_same_output(self) -> None:
        items = [
            ev("chunk", source_id="c1", doc_name="a.md", text="第一段"),
            ev("entity", source_id="项目A", text="实体描述"),
            ev("chunk", source_id="c2", doc_name="b.md", text="第二段"),
        ]

        first = build_context(question="问题", evidence=items)
        second = build_context(question="问题", evidence=items)

        self.assertEqual(first, second)
        self.assertEqual(first.version, CONTEXT_VERSION)

    def test_question_is_not_copied_into_context(self) -> None:
        built = build_context(question="机密问题", evidence=[ev(text="正文")])

        self.assertNotIn("机密问题", built.context_text)
        self.assertEqual(built.trace["question_chars"], len("机密问题"))

    def test_empty_evidence_is_valid(self) -> None:
        built = build_context(question="问题", evidence=[])

        self.assertEqual(built.context_text, "")
        self.assertEqual(built.citation_map, {})
        self.assertEqual(built.token_estimate, 0)
        self.assertEqual(built.dropped, [])
        self.assertIn("引用", built.system_prompt)


class GroupOrderTests(unittest.TestCase):
    def test_groups_are_rendered_in_fixed_order(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("relationship", source_id="r", text="关系"),
                ev("chunk", source_id="c", text="片段"),
                ev("entity", source_id="e", text="实体"),
            ],
        )

        text = built.context_text
        self.assertLess(text.index("## 资料片段"), text.index("## 实体"))
        self.assertLess(text.index("## 实体"), text.index("## 关系"))
        self.assertEqual(
            list(built.groups), ["chunk", "entity", "relationship"]
        )

    def test_input_order_is_preserved_within_a_group(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("chunk", source_id="c1", text="第一条"),
                ev("chunk", source_id="c2", text="第二条"),
            ],
        )

        self.assertLess(built.context_text.index("第一条"), built.context_text.index("第二条"))

    def test_unknown_kind_goes_last(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("web", source_id="w1", text="网页"),
                ev("chunk", source_id="c1", text="片段"),
            ],
        )

        self.assertLess(built.context_text.index("## 资料片段"), built.context_text.index("## web"))


class CitationTests(unittest.TestCase):
    def test_reference_id_is_inherited(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("chunk", source_id="c1", metadata={"reference_id": "3"}, text="正文")
            ],
        )

        citation = built.citation_map["c1"]
        self.assertEqual(citation.label, "[3]")
        self.assertEqual(citation.reference_id, "3")
        self.assertIn("[3]", built.context_text)

    def test_non_numeric_reference_id_is_kept(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("chunk", source_id="c1", metadata={"reference_id": "r1"}, text="正文")
            ],
        )

        self.assertEqual(built.citation_map["c1"].label, "[r1]")

    def test_fallback_label_without_reference_id(self) -> None:
        built = build_context(
            question="q",
            evidence=[ev("chunk", source_id="c1", text="正文")],
        )

        self.assertEqual(built.citation_map["c1"].label, "[chunk:c1]")

    def test_multiple_chunks_share_reference_id_without_losing_text(self) -> None:
        """同一文档的多个片段共享 [3]，但每条正文都要留下（按 reference_id 去重会吞正文）。"""
        built = build_context(
            question="q",
            evidence=[
                ev("chunk", source_id="c1", metadata={"reference_id": "3"}, text="第一段正文"),
                ev("chunk", source_id="c2", metadata={"reference_id": "3"}, text="第二段正文"),
            ],
        )

        self.assertEqual(len(built.citation_map), 2)
        self.assertEqual({c.label for c in built.citation_map.values()}, {"[3]"})
        self.assertIn("第一段正文", built.context_text)
        self.assertIn("第二段正文", built.context_text)

    def test_reference_merges_into_existing_chunks(self) -> None:
        chunk = ev("chunk", source_id="c1", metadata={"reference_id": "3"}, text="片段正文")
        reference = ev(
            "reference",
            evidence_id="reference:3",
            source_id="3",
            doc_name="a.md",
            metadata={
                "reference_id": "3",
                "gallery": {
                    "caption": "图片描述",
                    "image_id": "img:f1:0",
                    "asset_paths": ["source.png"],
                },
            },
        )

        built = build_context(question="q", evidence=[chunk, reference])

        self.assertEqual(list(built.citation_map), ["c1"])
        citation = built.citation_map["c1"]
        self.assertEqual(citation.image_id, "img:f1:0")
        self.assertEqual(citation.asset_paths, ("source.png",))
        self.assertEqual(built.context_text.count("片段正文"), 1)
        self.assertIn("描述：图片描述", built.context_text)
        self.assertEqual(built.trace["merged"], 1)

    def test_document_image_info_is_rendered_once_for_repeated_chunks(self) -> None:
        gallery = {
            "caption": "书桌描述",
            "image_id": "img:f1:0",
            "asset_paths": ["source.png"],
        }
        items = [
            ev(
                "chunk",
                evidence_id="chunk:c1",
                source_id="c1",
                metadata={"reference_id": "3", "gallery": gallery},
                text="第一段",
            ),
            ev(
                "chunk",
                evidence_id="chunk:c2",
                source_id="c2",
                metadata={"reference_id": "3", "gallery": gallery},
                text="第二段",
            ),
        ]

        built = build_context(question="q", evidence=items)

        self.assertEqual(built.context_text.count("描述：书桌描述"), 1)
        self.assertEqual(built.context_text.count("原图：source.png"), 1)
        self.assertEqual(built.citation_map["c1"].image_id, "img:f1:0")
        self.assertEqual(built.citation_map["c2"].image_id, "")

    def test_reference_without_chunks_is_rendered(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev(
                    "reference",
                    evidence_id="reference:9",
                    source_id="9",
                    doc_name="b.md",
                    metadata={"reference_id": "9"},
                )
            ],
        )

        self.assertIn("## 参考资料", built.context_text)
        self.assertIn("[9]", built.context_text)

    def test_standalone_image_label_and_paths(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev(
                    "image",
                    evidence_id="image:1",
                    doc_name="书桌照片.jpg",
                    image_id="img:f1:0",
                    text="识别文字",
                    metadata={
                        "gallery": {
                            "caption": "书桌描述",
                            "asset_paths": ["source.png"],
                        }
                    },
                )
            ],
        )

        citation = built.citation_map["img:f1:0"]
        self.assertEqual(citation.label, "[img-1]")
        self.assertIn("## 图片资料", built.context_text)
        self.assertIn("描述：书桌描述", built.context_text)
        self.assertIn("原图：source.png", built.context_text)

    def test_document_label(self) -> None:
        built = build_context(
            question="q",
            evidence=[ev("document", evidence_id="doc:1", doc_name="资料.md")],
        )

        self.assertEqual(built.citation_map["doc:1"].label, "[doc-1]")

    def test_duplicate_evidence_id_is_dropped(self) -> None:
        item = ev("chunk", evidence_id="chunk:c1", source_id="c1", text="正文")

        built = build_context(question="q", evidence=[item, item])

        self.assertEqual(built.context_text.count("正文"), 1)
        self.assertIn({"evidence_id": "chunk:c1", "reason": "duplicate"}, built.dropped)


class ReferenceMergeCompatibilityTests(unittest.TestCase):
    def test_reference_source_id_merges_when_metadata_missing(self) -> None:
        """真实适配器的 reference 只有 source_id；它必须能并进同编号 chunk。"""
        chunk = ev(
            "chunk",
            source_id="c1",
            doc_name="a.md",
            text="片段正文",
            metadata={"reference_id": "3"},
        )
        reference = ev(
            "reference",
            evidence_id="reference:3",
            source_id="3",
            doc_name="a.md",
            text="参考正文",
        )

        built = build_context(question="q", evidence=[chunk, reference])

        self.assertEqual(built.trace["merged"], 1)
        self.assertEqual(list(built.citation_map), ["c1"])
        self.assertEqual(built.citation_map["c1"].label, "[3]")
        self.assertNotIn("参考正文", built.context_text)
        self.assertNotIn("[reference:3]", built.context_text)
        self.assertNotIn("## 参考资料", built.context_text)

    def test_adapter_reference_shape_merges_end_to_end(self) -> None:
        """用 P0 适配器的真实输出形状，防止测试自造 metadata 掩盖集成问题。"""
        payload = {
            "data": {
                "chunks": [
                    {
                        "chunk_id": "c1",
                        "reference_id": "3",
                        "file_path": "a.md",
                        "content": "片段正文",
                    }
                ],
                "references": [
                    {
                        "reference_id": "3",
                        "file_path": "a.md",
                        "content": "参考正文",
                    }
                ],
            }
        }
        items = evidence_from_query_data(payload)
        self.assertEqual([item.kind for item in items], ["chunk", "reference"])

        built = build_context(question="q", evidence=items)

        self.assertEqual(built.trace["merged"], 1)
        self.assertEqual(list(built.citation_map), ["c1"])
        self.assertEqual(built.citation_map["c1"].label, "[3]")
        self.assertNotIn("参考正文", built.context_text)
        self.assertNotIn("[reference:3]", built.context_text)


class BudgetTests(unittest.TestCase):
    def test_per_item_chars_truncates(self) -> None:
        built = build_context(
            question="q",
            evidence=[ev("chunk", source_id="c1", doc_name="a.md", text="正" * 50)],
            budget=TokenBudget(total=1000, per_item_chars=10, max_items=10),
        )

        self.assertIn("正" * 10, built.context_text)
        self.assertNotIn("正" * 11, built.context_text)
        self.assertEqual(built.trace["truncated"], 1)

    def test_max_items_stops_the_tail(self) -> None:
        items = [
            ev("chunk", evidence_id=f"chunk:c{i}", source_id=f"c{i}", text=f"正文{i}")
            for i in range(5)
        ]

        built = build_context(
            question="q",
            evidence=items,
            budget=TokenBudget(total=1000, per_item_chars=100, max_items=2),
        )

        self.assertEqual(built.trace["included_count"], 2)
        self.assertIn("正文0", built.context_text)
        self.assertNotIn("正文4", built.context_text)
        self.assertEqual(
            [entry["reason"] for entry in built.dropped].count("max_items"), 3
        )

    def test_total_budget_is_not_exceeded(self) -> None:
        item = ev(
            "chunk",
            evidence_id="chunk:c1",
            source_id="c1",
            doc_name="a.md",
            text="正" * 10,
            metadata={"reference_id": "3"},
        )
        probe = build_context(
            question="q",
            evidence=[item],
            budget=TokenBudget(total=1000, per_item_chars=800, max_items=10),
        )
        self.assertEqual(probe.trace["included_count"], 1)
        exact = probe.token_estimate
        self.assertGreater(exact, 0)

        # 预算必须按最终 context_text（含 `## 组标题`）计算，不能只算片段。
        for total in range(0, exact + 1):
            built = build_context(
                question="q",
                evidence=[item],
                budget=TokenBudget(total=total, per_item_chars=800, max_items=10),
            )
            self.assertLessEqual(built.token_estimate, total)
            if total < exact:
                self.assertEqual(built.trace["included_count"], 0)

        at_limit = build_context(
            question="q",
            evidence=[item],
            budget=TokenBudget(total=exact, per_item_chars=800, max_items=10),
        )
        self.assertEqual(at_limit.trace["included_count"], 1)
        self.assertLessEqual(at_limit.token_estimate, exact)

        below = build_context(
            question="q",
            evidence=[item],
            budget=TokenBudget(total=exact - 1, per_item_chars=800, max_items=10),
        )
        self.assertTrue(any(entry["reason"] == "budget" for entry in below.dropped))

    def test_dropped_never_contains_body_text(self) -> None:
        secret = "机密正文内容"

        built = build_context(
            question="q",
            evidence=[ev("chunk", source_id="c1", text=secret * 100)],
            budget=TokenBudget(total=1, per_item_chars=1000, max_items=10),
        )

        self.assertTrue(built.dropped)
        self.assertNotIn(secret, json.dumps(built.dropped, ensure_ascii=False))

    def test_trace_never_contains_body_text(self) -> None:
        secret = "机密正文内容"

        built = build_context(
            question="q",
            evidence=[ev("chunk", source_id="c1", text=secret)],
        )

        rendered = json.dumps(built.trace, ensure_ascii=False, default=str)
        self.assertNotIn(secret, rendered)
        self.assertEqual(built.trace["included_count"], 1)

    def test_invalid_items_are_dropped_not_raised(self) -> None:
        built = build_context(
            question="q",
            evidence=[None, "not-evidence", ev("chunk", source_id="c1", text="正文")],
        )

        self.assertEqual(built.trace["invalid_count"], 2)
        self.assertEqual(built.trace["included_count"], 1)
        self.assertEqual(
            [entry["reason"] for entry in built.dropped].count("invalid"), 2
        )

    def test_budget_validation(self) -> None:
        with self.assertRaises(ValueError):
            TokenBudget(total=-1)
        with self.assertRaises(ValueError):
            TokenBudget(per_item_chars=0)
        with self.assertRaises(ValueError):
            TokenBudget(max_items=-1)


class BadInputTests(unittest.TestCase):
    def test_none_optional_string_fields_degrade(self) -> None:
        """source_id/doc_name 等可选字段为 None 时降级，不抛 AttributeError。"""
        item = Evidence(
            evidence_id="chunk:c1",
            kind="chunk",
            source_id=None,
            doc_name=None,
            file_id=None,
            image_id=None,
            text="正文",
            metadata={"reference_id": "3"},
        )

        built = build_context(question="q", evidence=[item])

        self.assertEqual(built.trace["included_count"], 1)
        self.assertEqual(built.citation_map["chunk:c1"].label, "[3]")
        self.assertIn("正文", built.context_text)


class RuleTests(unittest.TestCase):
    def test_require_citations_false_hides_labels_but_keeps_map(self) -> None:
        built = build_context(
            question="q",
            evidence=[
                ev("chunk", source_id="c1", metadata={"reference_id": "3"}, text="正文")
            ],
            rules=RuleSet(require_citations=False),
        )

        self.assertNotIn("[3]", built.context_text)
        self.assertIn("c1", built.citation_map)

    def test_output_format_is_added_to_system_prompt(self) -> None:
        built = build_context(
            question="q",
            evidence=[],
            output_format="只输出一个 Markdown 表格",
        )

        self.assertIn("只输出一个 Markdown 表格", built.system_prompt)
        self.assertIn("输出格式要求", built.system_prompt)

    def test_custom_prompt_gets_citation_rule(self) -> None:
        built = build_context(
            question="q",
            evidence=[],
            rules=RuleSet(system_prompt="自定义身份"),
        )

        self.assertIn("自定义身份", built.system_prompt)
        self.assertIn("引用资料时使用上下文中的方括号编号", built.system_prompt)


class EstimateTests(unittest.TestCase):
    def test_estimate_tokens_is_conservative(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("中文"), 2)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("中a"), 2)


GALLERY_ENTRY = {
    "doc_name": "书桌照片_abcdef.md",
    "file_id": "f1",
    "name": "书桌照片.jpg",
    "sha256": "abcdef",
    "at": "2026-10-17 00:00:00",
    "evidence": {
        "version": 1,
        "kind": "image",
        "image_id": "img:f1:0",
        "caption": "书桌描述",
        "ocr_preview": "识别预览",
        "ocr_chars": 4,
        "asset_paths": ["source.png"],
        "keywords": ["书桌"],
        "result_pointer": "parsed/f1/result.json",
        "content_pointer": "parsed/f1/content.md",
    },
}


class GalleryEnrichmentTests(unittest.TestCase):
    def test_hit_adds_fields_without_mutating_input(self) -> None:
        original = ev("chunk", source_id="c1", doc_name="书桌照片_abcdef.md", text="片段")

        enriched = enrich_with_gallery([original], lambda name: GALLERY_ENTRY)

        self.assertIsNot(enriched[0], original)
        self.assertEqual(original.file_id, "")
        self.assertEqual(enriched[0].file_id, "f1")
        self.assertEqual(enriched[0].image_id, "img:f1:0")
        self.assertEqual(enriched[0].metadata["gallery"]["caption"], "书桌描述")
        self.assertEqual(enriched[0].metadata["gallery"]["asset_paths"], ["source.png"])

    def test_miss_returns_original(self) -> None:
        original = ev("chunk", source_id="c1", doc_name="不存在.md", text="片段")

        enriched = enrich_with_gallery([original], lambda name: None)

        self.assertEqual(enriched, [original])

    def test_lookup_failure_degrades(self) -> None:
        original = ev("chunk", source_id="c1", doc_name="a.md", text="片段")

        def boom(name: str):
            raise RuntimeError("索引坏了")

        enriched = enrich_with_gallery([original], boom)

        self.assertEqual(enriched, [original])

    def test_lookup_is_cached_per_document(self) -> None:
        calls = []

        def lookup(name: str):
            calls.append(name)
            return GALLERY_ENTRY

        items = [
            ev("chunk", evidence_id="chunk:c1", source_id="c1", doc_name="书桌照片_abcdef.md"),
            ev("chunk", evidence_id="chunk:c2", source_id="c2", doc_name="书桌照片_abcdef.md"),
        ]

        enrich_with_gallery(items, lookup)

        self.assertEqual(calls, ["书桌照片_abcdef.md"])

    def test_no_lookup_or_non_evidence_is_safe(self) -> None:
        items = [ev("chunk", source_id="c1", doc_name="a.md"), "raw"]

        self.assertEqual(enrich_with_gallery(items, None), items)
        self.assertEqual(enrich_with_gallery([], lambda name: GALLERY_ENTRY), [])

    def test_enriched_evidence_reaches_the_citation_and_context(self) -> None:
        items = [
            ev(
                "chunk",
                source_id="c1",
                doc_name="书桌照片_abcdef.md",
                text="片段正文",
                metadata={"reference_id": "3"},
            )
        ]

        enriched = enrich_with_gallery(items, lambda name: GALLERY_ENTRY)
        built = build_context(question="q", evidence=enriched)

        citation = built.citation_map["c1"]
        self.assertEqual(citation.label, "[3]")
        self.assertEqual(citation.file_id, "f1")
        self.assertEqual(citation.image_id, "img:f1:0")
        self.assertEqual(citation.asset_paths, ("source.png",))
        self.assertIn("描述：书桌描述", built.context_text)
        self.assertIn("原图：source.png", built.context_text)
        self.assertEqual(built.trace["gallery_matches"], 1)


if __name__ == "__main__":
    unittest.main()
