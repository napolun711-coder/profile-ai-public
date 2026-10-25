"""P2C：`image.search` 工具契约与只读行为（全离线）。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.plugins import check_schema, load_builtin_tools
from app.plugins.adapters.image import SPEC, TOOL, ImageSearchTool
from app.services import gallery, image_evidence, ingest


def make_index(items: dict, *, version: int = 2) -> None:
    path = gallery.index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": version, "updated_at": "2026-10-17 00:00:00", "items": items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def image_entry(file_id: str, doc_name: str) -> dict:
    return {
        "doc_name": doc_name,
        "file_id": file_id,
        "name": "书桌照片.jpg",
        "evidence": {
            "version": 1,
            "kind": "image",
            "image_id": f"img:{file_id}:0",
            "caption": "",
            "ocr_preview": "桌上有台灯。",
            "asset_paths": ["source.jpg"],
            "keywords": ["书桌照片", "台灯"],
        },
    }


class ImageToolFixture(unittest.TestCase):
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


class ContractTests(unittest.TestCase):
    def test_spec_is_valid_and_not_builtin(self) -> None:
        self.assertEqual(SPEC.name, "image.search")
        self.assertEqual(SPEC.side_effect, "read_local")
        self.assertFalse(SPEC.external)
        self.assertTrue(SPEC.allowed_for_skills)
        self.assertNotIn("image.search", load_builtin_tools().names())

    def test_tool_instance_uses_spec(self) -> None:
        tool = ImageSearchTool()
        self.assertIs(tool.spec, SPEC)
        self.assertTrue(callable(tool.invoke))
        self.assertTrue(callable(tool.health))


class InvokeTests(ImageToolFixture):
    def test_success_returns_data_and_evidence(self) -> None:
        make_index({"书桌照片_abcdef.md": image_entry("f1", "书桌照片_abcdef.md")})

        result = TOOL.invoke(query="找一下书桌", limit=8)

        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 1)
        self.assertEqual(result.data["matched"], 1)
        self.assertEqual(result.data["returned"], 1)
        self.assertFalse(result.data["truncated"])
        self.assertEqual(result.data["items"][0]["file_id"], "f1")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].kind, "image")
        self.assertEqual(check_schema(SPEC.output_schema, result.data), [])

    def test_v1_index_is_derived_without_writing(self) -> None:
        directory = self.parsed / "f1"
        directory.mkdir(parents=True)
        (directory / "content.md").write_text("识别正文。", encoding="utf-8")
        (directory / "source.jpg").write_bytes(b"x")
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "file_id": "f1",
                    "route": "image",
                    "doc_name": "书桌照片_abcdef.md",
                    "name": "书桌照片.jpg",
                    "source_path": str(directory / "source.jpg"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        make_index(
            {"书桌照片_abcdef.md": {"doc_name": "书桌照片_abcdef.md", "file_id": "f1"}},
            version=1,
        )
        before = gallery.index_path().read_bytes()

        result = TOOL.invoke(query="书桌照片")

        self.assertTrue(result.ok)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].metadata["gallery"]["ocr_preview"], "识别正文。")
        self.assertEqual(gallery.index_path().read_bytes(), before)

    def test_empty_index_is_a_success(self) -> None:
        result = TOOL.invoke(query="找一下图片")

        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 0)
        self.assertEqual(result.evidence, [])

    def test_index_is_not_modified(self) -> None:
        make_index({"书桌照片_abcdef.md": image_entry("f1", "书桌照片_abcdef.md")})
        before = hashlib.sha256(gallery.index_path().read_bytes()).hexdigest()

        TOOL.invoke(query="书桌照片")

        after = hashlib.sha256(gallery.index_path().read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_invalid_inputs_do_not_call_search(self) -> None:
        with mock.patch.object(image_evidence, "search_local") as search:
            for query, limit in (
                ("ab", None),
                (123, None),
                ("书桌照片", True),
                ("书桌照片", 0),
                ("书桌照片", image_evidence.MAX_LIMIT + 1),
                ("书桌照片", "3"),
            ):
                with self.subTest(query=query, limit=limit):
                    result = TOOL.invoke(query=query, limit=limit)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error.code, "invalid_input")
            search.assert_not_called()

    def test_exception_message_is_sanitized(self) -> None:
        with mock.patch.object(
            image_evidence, "search_local", side_effect=RuntimeError("机密正文")
        ):
            result = TOOL.invoke(query="书桌照片")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "internal_error")
        self.assertIn("RuntimeError", result.error.message)
        self.assertNotIn("机密正文", result.error.message)
        self.assertEqual(result.error.detail, "机密正文")


class HealthTests(ImageToolFixture):
    def test_health_reports_missing_index(self) -> None:
        health = TOOL.health()

        self.assertFalse(health.ok)
        self.assertIn("不存在", health.detail)

    def test_health_reports_readable_index(self) -> None:
        make_index({})

        health = TOOL.health()

        self.assertTrue(health.ok)
        self.assertIn("可读", health.detail)

    def test_health_reports_corrupt_index(self) -> None:
        path = gallery.index_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        for payload in ("{not valid json", '{"items": []}'):
            with self.subTest(payload=payload):
                path.write_text(payload, encoding="utf-8")

                health = TOOL.health()

                self.assertFalse(health.ok)
                self.assertIn("损坏", health.detail)


if __name__ == "__main__":
    unittest.main()
