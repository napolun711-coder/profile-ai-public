"""P1B：证据 v1 的构建规则（纯派生数据，离线用例）。

约定：作业目录换到临时目录；不联网、不读写真实 `data/`。
重点钉住三件事：
1. 描述来自 `CAPTION_MARK` 之后，预览不含描述、不含标记/标签；
2. 资源路径必须是**作业目录内真实存在**的文件，`..` / 绝对路径 / URL 一律丢弃；
3. 输出有上限（描述 2000、预览 200、关键词 8、路径 20），坏输入不抛异常。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import evidence, ingest

CAPTION = "书桌照片：2026 年 10 月拍的，桌上有台灯和两摞资料。"


class EvidenceFixture(unittest.TestCase):
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

    def job(self, file_id: str = "20261017000000-abcdef12") -> Path:
        directory = self.parsed / file_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write_job(
        self,
        *,
        file_id: str = "20261017000000-abcdef12",
        route: str = "image",
        name: str = "书桌照片.jpg",
        content: str = "",
        source: bool = True,
    ) -> dict:
        directory = self.job(file_id)
        (directory / "content.md").write_text(content, encoding="utf-8")
        data = {
            "file_id": file_id,
            "route": route,
            "name": name,
            "doc_name": "",
            "assets": [],
            "caption": {"text": ""},
        }
        if source:
            suffix = Path(name).suffix or ".bin"
            source_path = directory / f"source{suffix}"
            source_path.write_bytes(b"x")
            data["source_path"] = str(source_path)
        return data


class ImageEvidenceTests(EvidenceFixture):
    def test_image_evidence_shape(self) -> None:
        data = self.write_job(
            content=f"识别出的正文。\n\n{ingest.CAPTION_MARK}\n\n{CAPTION}"
        )

        result = evidence.from_job(data)

        self.assertIsNotNone(result)
        self.assertEqual(result["version"], evidence.EVIDENCE_VERSION)
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["image_id"], f"img:{data['file_id']}:0")
        self.assertEqual(result["caption"], CAPTION)
        self.assertEqual(result["ocr_preview"], "识别出的正文。")
        self.assertEqual(result["ocr_chars"], len("识别出的正文。"))
        self.assertEqual(result["asset_paths"], ["source.jpg"])
        self.assertIn("书桌照片", result["keywords"])
        self.assertEqual(result["result_pointer"], f"parsed/{data['file_id']}/result.json")
        self.assertEqual(result["content_pointer"], f"parsed/{data['file_id']}/content.md")

    def test_caption_is_not_part_of_the_preview(self) -> None:
        data = self.write_job(
            content=f"OCR 正文。\n\n{ingest.CAPTION_MARK}\n\n{CAPTION}"
        )

        result = evidence.from_job(data)

        self.assertNotIn("台灯", result["ocr_preview"])
        self.assertNotIn("<!--", result["ocr_preview"])

    def test_image_without_text_is_still_valid(self) -> None:
        data = self.write_job(content="")

        result = evidence.from_job(data)

        self.assertEqual(result["caption"], "")
        self.assertEqual(result["ocr_preview"], "")
        self.assertEqual(result["ocr_chars"], 0)
        self.assertEqual(result["asset_paths"], ["source.jpg"])

    def test_caption_falls_back_to_result_json(self) -> None:
        data = self.write_job(content="没有描述标记的正文。")
        data["caption"] = {"text": "档案里留着的描述"}

        result = evidence.from_job(data)

        self.assertEqual(result["caption"], "档案里留着的描述")
        self.assertEqual(result["ocr_preview"], "没有描述标记的正文。")

    def test_caption_is_capped(self) -> None:
        long_caption = "长" * (evidence.CAPTION_MAX_CHARS + 500)
        data = self.write_job(
            content=f"正文。\n\n{ingest.CAPTION_MARK}\n\n{long_caption}"
        )

        result = evidence.from_job(data)

        self.assertEqual(len(result["caption"]), evidence.CAPTION_MAX_CHARS)

    def test_preview_is_plain_and_capped(self) -> None:
        body = "<div>开头</div><!-- 注释 -->\n\n" + "正" * 300
        data = self.write_job(content=body)

        result = evidence.from_job(data)

        self.assertLessEqual(len(result["ocr_preview"]), evidence.PREVIEW_MAX_CHARS)
        self.assertNotIn("<div>", result["ocr_preview"])
        self.assertNotIn("注释", result["ocr_preview"])
        self.assertTrue(result["ocr_preview"].startswith("开头"))

    def test_injected_content_wins_over_disk(self) -> None:
        data = self.write_job(content="磁盘上的正文")

        result = evidence.from_job(
            data, content=f"注入的正文\n\n{ingest.CAPTION_MARK}\n\n注入的描述"
        )

        self.assertEqual(result["ocr_preview"], "注入的正文")
        self.assertEqual(result["caption"], "注入的描述")


class DocumentEvidenceTests(EvidenceFixture):
    def test_document_has_no_image_fields(self) -> None:
        data = self.write_job(route="text", name="项目计划.txt", content="正文内容。")

        result = evidence.from_job(data)

        self.assertEqual(result["kind"], "document")
        self.assertEqual(result["image_id"], "")
        self.assertEqual(result["caption"], "")
        self.assertEqual(result["ocr_preview"], "")
        self.assertEqual(result["ocr_chars"], 0)
        self.assertEqual(result["asset_paths"], ["source.txt"])
        self.assertIn("项目计划", result["keywords"])

    def test_document_caption_still_uses_the_mark(self) -> None:
        data = self.write_job(
            route="pdf",
            name="扫描件.pdf",
            content=f"正文。\n\n{ingest.CAPTION_MARK}\n\n补充说明。",
        )

        result = evidence.from_job(data)

        self.assertEqual(result["caption"], "补充说明。")
        self.assertEqual(result["ocr_preview"], "")


class AssetPathTests(EvidenceFixture):
    def test_unsafe_and_missing_refs_are_dropped(self) -> None:
        data = self.write_job()
        directory = self.job(data["file_id"])
        (directory / "assets").mkdir()
        (directory / "assets" / "00_ok.png").write_bytes(b"x")
        outside = self.tmp / "outside.png"
        outside.write_bytes(b"x")
        (directory / "content.md").write_text(
            "\n".join(
                [
                    '<img src="assets/00_ok.png">',
                    '<img src="./assets/00_ok.png">',
                    '<img src="imgs/missing.jpg">',
                    '<img src="../outside.png">',
                    '<img src="/absolute.png">',
                    '<img src="https://example.com/a.png">',
                    '<img src="C:/Windows/a.png">',
                ]
            ),
            encoding="utf-8",
        )
        data["assets"] = [
            {"path": "assets/00_ok.png"},
            {"path": "../outside.png"},
            {"path": None},
        ]

        result = evidence.from_job(data)

        self.assertEqual(result["asset_paths"], ["source.jpg", "assets/00_ok.png"])

    def test_paths_are_deduped_and_capped(self) -> None:
        data = self.write_job()
        directory = self.job(data["file_id"])
        (directory / "assets").mkdir()
        for index in range(30):
            (directory / "assets" / f"{index:02d}.png").write_bytes(b"x")
        data["assets"] = [
            {"path": f"assets/{index:02d}.png"} for index in range(30)
        ]

        result = evidence.from_job(data)

        self.assertEqual(len(result["asset_paths"]), evidence.ASSET_PATHS_MAX)
        self.assertEqual(len(set(result["asset_paths"])), len(result["asset_paths"]))
        self.assertEqual(result["asset_paths"][0], "source.jpg")

    def test_file_outside_the_job_directory_is_rejected(self) -> None:
        data = self.write_job()
        outside = self.tmp / "outside.png"
        outside.write_bytes(b"x")
        data["assets"] = [{"path": str(outside)}]

        result = evidence.from_job(data)

        self.assertEqual(result["asset_paths"], ["source.jpg"])


class KeywordTests(unittest.TestCase):
    def test_keywords_are_deterministic_deduped_and_bounded(self) -> None:
        # 调用方传入的是名字部分（from_job 已经剥过扩展名与短 ID）
        first = evidence.keywords("书桌照片", "书桌照片", CAPTION)
        second = evidence.keywords("书桌照片", "书桌照片", CAPTION)

        self.assertEqual(first, second)
        self.assertEqual(first[0], "书桌照片")
        self.assertLessEqual(len(first), evidence.KEYWORDS_MAX)
        self.assertEqual(len(first), len(set(tag.lower() for tag in first)))

    def test_short_and_overlong_tokens_are_dropped(self) -> None:
        tags = evidence.keywords("a 12 " + "长" * 21 + " 正常的词", limit=8)

        self.assertNotIn("a", tags)
        self.assertIn("12", tags)
        self.assertNotIn("长" * 21, tags)
        self.assertIn("正常的词", tags)

    def test_limit_zero_returns_empty(self) -> None:
        self.assertEqual(evidence.keywords("书桌照片", limit=0), [])


class RobustnessTests(EvidenceFixture):
    def test_missing_file_id_returns_none(self) -> None:
        self.assertIsNone(evidence.from_job({}))
        self.assertIsNone(evidence.from_job(None))
        self.assertIsNone(evidence.from_job({"file_id": "  "}))

    def test_weird_fields_never_raise(self) -> None:
        data = {
            "file_id": "20261017000000-abcdef12",
            "route": None,
            "name": None,
            "doc_name": 123,
            "assets": "not-a-list",
            "caption": "not-a-dict",
            "source_path": 456,
        }

        result = evidence.from_job(data, content="")

        self.assertEqual(result["kind"], "document")
        self.assertEqual(result["asset_paths"], [])

    def test_missing_job_directory_degrades_to_no_assets(self) -> None:
        data = {"file_id": "does-not-exist", "route": "image", "name": "x.jpg"}

        result = evidence.from_job(data, content="正文")

        self.assertEqual(result["asset_paths"], [])


if __name__ == "__main__":
    unittest.main()
