"""P2C：图片证据第二通道的离线单测。

约定：`ingest.PARSED_DIR` 换到临时目录；不联网、不读写真实 `data/`。
重点：
1. 本地匹配只用 gallery 证据，v1 条目内存派生且**不回写**；
2. `/query/data` 图片候选按文档名分类、同图聚合；
3. 两通道按配额合并、按 image_id 去重；
4. 输出能被 P1C Context Builder 直接渲染。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.plugins import Evidence
from app.services import gallery, image_evidence, ingest
from app.services.context_builder import TokenBudget, build_context


def image_entry(
    file_id: str,
    doc_name: str,
    *,
    caption: str = "",
    preview: str = "",
    keywords=None,
    assets=None,
    kind: str = "image",
) -> dict:
    image_id = f"img:{file_id}:0" if kind == "image" else ""
    return {
        "doc_name": doc_name,
        "file_id": file_id,
        "name": f"{doc_name}.jpg",
        "sha256": "abcdef123456",
        "at": "2026-10-17 00:00:00",
        "evidence": {
            "version": 1,
            "kind": kind,
            "image_id": image_id,
            "caption": caption,
            "ocr_preview": preview,
            "ocr_chars": len(preview),
            "asset_paths": list(assets or []),
            "keywords": list(keywords or []),
            "result_pointer": f"parsed/{file_id}/result.json",
            "content_pointer": f"parsed/{file_id}/content.md",
        },
    }


class ImageEvidenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.parsed = self.tmp / "parsed"
        self.parsed.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(ingest, "PARSED_DIR", self.parsed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_index(self, items: dict, *, version: int = 2) -> str:
        path = gallery.index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": version, "updated_at": "2026-10-17 00:00:00", "items": items},
            ensure_ascii=False,
            indent=2,
        )
        path.write_text(payload, encoding="utf-8")
        return payload

    def make_job(
        self,
        file_id: str,
        *,
        route: str = "image",
        doc_name: str = "书桌照片_abcdef.md",
        content: str = "",
    ) -> dict:
        directory = self.parsed / file_id
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "source.jpg"
        source.write_bytes(b"x")
        (directory / "content.md").write_text(content, encoding="utf-8")
        data = {
            "file_id": file_id,
            "route": route,
            "doc_name": doc_name,
            "name": "书桌照片.jpg",
            "sha256": "abcdef123456",
            "state": ingest.STATE_DONE,
            "assets": [],
            "caption": {"text": ""},
            "source_path": str(source),
        }
        (directory / "result.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return data


class LocalSearchTests(ImageEvidenceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.items = {
            "书桌照片_abcdef.md": image_entry(
                "f1",
                "书桌照片_abcdef.md",
                preview="桌上有台灯和两摞资料。",
                keywords=["书桌照片", "台灯", "资料"],
                assets=["source.jpg"],
            ),
            "窗外照片_123456.md": image_entry(
                "f2",
                "窗外照片_123456.md",
                preview="窗外是下雨的街道。",
                keywords=["窗外照片", "街道"],
                assets=["source.jpg"],
            ),
        }

    def test_local_search_returns_image_evidence(self) -> None:
        items, stats = image_evidence.search_local("找一下书桌", items=self.items)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "image")
        self.assertEqual(items[0].file_id, "f1")
        self.assertEqual(items[0].image_id, "img:f1:0")
        self.assertEqual(items[0].doc_name, "书桌照片_abcdef.md")
        self.assertEqual(items[0].text, "桌上有台灯和两摞资料。")
        self.assertEqual(items[0].origin_url, "/api/files/raw?file_id=f1")
        self.assertEqual(items[0].metadata["gallery"]["asset_paths"], ["source.jpg"])
        self.assertEqual(items[0].metadata["gallery"]["keywords"][0], "书桌照片")
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["returned"], 1)
        self.assertFalse(stats["truncated"])

    def test_listing_query_returns_all_images(self) -> None:
        items, stats = image_evidence.search_local("有哪些图片", items=self.items)

        self.assertEqual(len(items), 2)
        self.assertTrue(stats["listing"])
        self.assertEqual(stats["reason"], "ok")

    def test_deictic_query_without_content_returns_empty(self) -> None:
        items, stats = image_evidence.search_local("这张图里有什么", items=self.items)

        self.assertEqual(items, [])
        self.assertEqual(stats["reason"], "no_terms")

    def test_limit_and_order_are_deterministic(self) -> None:
        first, first_stats = image_evidence.search_local(
            "图片 台灯 街道", items=self.items, limit=1
        )
        second, second_stats = image_evidence.search_local(
            "图片 台灯 街道", items=self.items, limit=1
        )

        self.assertEqual([item.evidence_id for item in first], ["image:img:f1:0"])
        self.assertEqual(
            [item.evidence_id for item in first],
            [item.evidence_id for item in second],
        )
        self.assertTrue(first_stats["truncated"])
        self.assertEqual(second_stats["returned"], 1)

    def test_document_entries_are_not_images(self) -> None:
        items = {
            "项目计划.md": image_entry(
                "d1", "项目计划.md", kind="document", keywords=["项目计划"]
            )
        }

        found, stats = image_evidence.search_local("项目", items=items)

        self.assertEqual(found, [])
        self.assertEqual(stats["total"], 0)

    def test_missing_file_id_is_skipped(self) -> None:
        items = {"坏条目.md": {"doc_name": "坏条目.md", "file_id": ""}}

        found, stats = image_evidence.search_local("图片", items=items)

        self.assertEqual(found, [])
        self.assertEqual(stats["total"], 0)


class ReadOnlyDerivationTests(ImageEvidenceFixture):
    def test_v1_entry_is_derived_in_memory_without_writing(self) -> None:
        content = f"识别正文。\n\n{ingest.CAPTION_MARK}\n\n书桌照片描述。"
        self.make_job("f1", content=content)
        before = self.write_index(
            {"书桌照片_abcdef.md": {"doc_name": "书桌照片_abcdef.md", "file_id": "f1"}},
            version=1,
        )

        items, stats = image_evidence.search_local("书桌", items=gallery.read_index()["items"])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].metadata["gallery"]["caption"], "书桌照片描述。")
        self.assertEqual(items[0].metadata["gallery"]["ocr_preview"], "识别正文。")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(gallery.index_path().read_text(encoding="utf-8"), before)
        self.assertEqual(json.loads(before)["version"], 1)

    def test_v1_document_entry_is_not_treated_as_image(self) -> None:
        self.make_job("d1", route="text", doc_name="项目计划.md", content="正文")
        self.write_index(
            {"项目计划.md": {"doc_name": "项目计划.md", "file_id": "d1"}},
            version=1,
        )

        items, _ = image_evidence.search_local("图片", items=gallery.read_index()["items"])

        self.assertEqual(items, [])


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = {
            "书桌照片_abcdef.md": image_entry(
                "f1",
                "书桌照片_abcdef.md",
                keywords=["书桌照片"],
                assets=["source.jpg"],
            ),
            "项目计划.md": image_entry(
                "d1", "项目计划.md", kind="document", keywords=["项目计划"]
            ),
        }
        self.mapping = image_evidence.image_map(self.items)

    def test_chunks_from_image_document_are_aggregated(self) -> None:
        chunks = [
            Evidence(
                evidence_id="chunk:c1",
                kind="chunk",
                source_id="c1",
                doc_name="书桌照片_abcdef.md",
                text="短正文",
                score=0.4,
                metadata={"reference_id": "1"},
            ),
            Evidence(
                evidence_id="chunk:c2",
                kind="chunk",
                source_id="c2",
                doc_name="书桌照片_abcdef.md",
                text="更长的正文片段",
                score=0.8,
                metadata={"reference_id": "1"},
            ),
            Evidence(
                evidence_id="chunk:t1",
                kind="chunk",
                source_id="t1",
                doc_name="项目计划.md",
                text="普通资料",
                metadata={"reference_id": "2"},
            ),
        ]

        others, images = image_evidence.classify_evidence(
            chunks, image_map=self.mapping
        )

        self.assertEqual([item.evidence_id for item in others], ["chunk:t1"])
        self.assertEqual(len(images), 1)
        image = images[0]
        self.assertEqual(image.evidence_id, "image:img:f1:0")
        self.assertEqual(image.text, "更长的正文片段")
        self.assertEqual(image.score, 0.8)
        self.assertEqual(image.metadata["channels"], ["retrieval"])
        self.assertEqual(image.metadata["chunk_ids"], ["c1", "c2"])

    def test_historical_short_id_mismatch_is_matched(self) -> None:
        items = {
            "屏幕截图_2026-07-20_234254_b20c0f.md": image_entry(
                "f9", "屏幕截图_2026-07-20_234254_b20c0f.md"
            )
        }
        mapping = image_evidence.image_map(items)
        chunk = Evidence(
            evidence_id="chunk:h1",
            kind="chunk",
            source_id="h1",
            doc_name="屏幕截图_2026-07-20_234254.md",
            text="历史文档片段",
        )

        others, images = image_evidence.classify_evidence(
            [chunk], image_map=mapping
        )

        self.assertEqual(others, [])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].file_id, "f9")

    def test_ambiguous_short_id_alias_is_not_used(self) -> None:
        items = {
            "截图_abcdef.md": image_entry("f1", "截图_abcdef.md"),
            "截图_123456.md": image_entry("f2", "截图_123456.md"),
        }
        mapping = image_evidence.image_map(items)
        chunk = Evidence(
            evidence_id="chunk:h2",
            kind="chunk",
            source_id="h2",
            doc_name="截图.md",
            text="无法判断是哪一张",
        )

        others, images = image_evidence.classify_evidence(
            [chunk], image_map=mapping
        )

        self.assertEqual(others, [chunk])
        self.assertEqual(images, [])

    def test_non_evidence_and_non_chunk_are_untouched(self) -> None:
        weird = object()
        entity = Evidence(
            evidence_id="entity:x",
            kind="entity",
            doc_name="书桌照片_abcdef.md",
        )

        others, images = image_evidence.classify_evidence(
            [weird, entity], image_map=self.mapping
        )

        self.assertEqual(others, [weird, entity])
        self.assertEqual(images, [])


class MergeTests(unittest.TestCase):
    def make(self, evidence_id: str, *, text: str = "", score: float = 0.5) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            kind="image",
            source_id=evidence_id,
            doc_name=f"{evidence_id}.md",
            file_id=evidence_id,
            image_id=f"img:{evidence_id}:0",
            text=text,
            score=score,
            metadata={"gallery": {"asset_paths": ["source.jpg"]}, "channels": ["gallery"]},
        )

    def test_merge_dedupes_and_unions_channels(self) -> None:
        a = self.make("a")
        b = self.make("b", text="本地正文")
        b_retrieval = self.make("b", text="更长的检索正文", score=0.9)
        b_retrieval.metadata["channels"] = ["retrieval"]
        c = self.make("c")

        merged = image_evidence.merge_images(
            [a, b], [b_retrieval, c], limit=3, local_quota=2, retrieval_quota=2
        )

        self.assertEqual([item.evidence_id for item in merged], ["a", "b", "c"])
        self.assertEqual(merged[1].text, "更长的检索正文")
        self.assertEqual(merged[1].score, 0.9)
        self.assertEqual(merged[1].metadata["channels"], ["gallery", "retrieval"])

    def test_limit_is_enforced(self) -> None:
        merged = image_evidence.merge_images(
            [self.make("a"), self.make("b")],
            [self.make("c"), self.make("d")],
            limit=2,
            local_quota=1,
            retrieval_quota=1,
        )

        self.assertEqual(len(merged), 2)

    def test_leftover_candidates_fill_to_limit(self) -> None:
        """首轮配额后允许余量补位；单通道候选可补满总数上限。"""
        retrieval = [self.make(f"r{index}") for index in range(10)]

        merged = image_evidence.merge_images(
            [],
            retrieval,
            limit=8,
            local_quota=5,
            retrieval_quota=5,
        )

        self.assertEqual(len(merged), 8)
        self.assertEqual(
            [item.evidence_id for item in merged],
            [f"r{index}" for index in range(8)],
        )


class ContextBuilderCompatibilityTests(ImageEvidenceFixture):
    def test_image_evidence_renders_with_description_and_original(self) -> None:
        items = {
            "书桌照片_abcdef.md": image_entry(
                "f1",
                "书桌照片_abcdef.md",
                caption="书桌描述正文",
                preview="OCR 预览正文",
                keywords=["书桌照片"],
                assets=["source.jpg"],
            )
        }
        evidence, _ = image_evidence.search_local("书桌", items=items)

        built = build_context(
            question="书桌照片是什么",
            evidence=evidence,
            budget=TokenBudget(total=1000, per_item_chars=200, max_items=5),
        )

        self.assertIn("image", built.groups)
        self.assertEqual(len(built.groups["image"]), 1)
        citation = next(iter(built.citation_map.values()))
        self.assertEqual(citation.label, "[img-1]")
        self.assertEqual(citation.caption, "书桌描述正文")
        self.assertIn("图片资料", built.context_text)
        self.assertIn("描述：书桌描述正文", built.context_text)
        self.assertIn("原图：source.jpg", built.context_text)


class RobustnessTests(unittest.TestCase):
    def test_bad_inputs_degrade_to_empty(self) -> None:
        self.assertEqual(image_evidence.search_local("", items={})[0], [])
        self.assertEqual(image_evidence.search_local(None, items={})[0], [])
        self.assertEqual(image_evidence.search_local("图片", items={"x": 1})[0], [])
        self.assertEqual(image_evidence.classify_evidence(None), ([], []))
        self.assertEqual(image_evidence.classify_evidence(123), ([], []))
        self.assertEqual(image_evidence.merge_images(None, None), [])
        self.assertEqual(image_evidence.merge_images(123, 123), [])

    def test_image_records_accept_weird_entries(self) -> None:
        self.assertEqual(image_evidence.image_records({"a": None, "b": []}), [])


if __name__ == "__main__":
    unittest.main()
