"""P0：三个内置适配器 + 证据归一化 + 影子脚本的单测（全离线）。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from app.clients import lightrag
from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError
from app.plugins import (
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_OCR_FAILED,
    ERROR_PARSE_FAILED,
    ERROR_UPSTREAM,
    Evidence,
    Health,
    ToolContext,
    ToolError,
    ToolResult,
)
from app.plugins.adapters import knowledge, ocr, parse
from app.services import ocr as ocr_service
from app.services import parse as parse_service
from tests import fixtures

PAYLOAD = {
    "status": "success",
    "message": "ok",
    "data": {
        "entities": [
            {
                "entity_name": "项目A",
                "entity_type": "项目",
                "description": "实体描述",
                "file_path": "资料.md",
                "score": 0.9,
            }
        ],
        "relationships": [
            {
                "src_id": "项目A",
                "tgt_id": "项目B",
                "description": "关系描述",
                "file_path": "资料.md",
                "weight": 0.5,
                "keywords": "合作",
            }
        ],
        "chunks": [
            {
                "chunk_id": "c1",
                "reference_id": "r1",
                "content": "片段正文",
                "file_path": "资料.md",
            }
        ],
        "references": [
            {"reference_id": "r1", "file_path": "资料.md", "content": "引用正文"}
        ],
    },
    "metadata": {"query_mode": "mix"},
}


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ParseToolTests(TempDirCase):
    def test_result_matches_parse_file(self) -> None:
        path = fixtures.make_text(self.tmp / "简历.md", "# 个人简历\n\n姓名：张伟")

        expected = parse_service.parse_file(path)
        result = parse.TOOL.invoke(None, path=path)

        self.assertTrue(result.ok)
        data = result.data
        self.assertEqual(data.ok, expected.ok)
        self.assertEqual(data.route, expected.route)
        self.assertEqual(data.text, expected.text)
        self.assertEqual(data.pages, expected.pages)
        self.assertEqual(data.images, expected.images)
        self.assertEqual(data.parser, expected.parser)
        self.assertEqual(data.detail, expected.detail)
        self.assertEqual(data.truncated, expected.truncated)
        self.assertEqual(
            [(asset.name, asset.source, asset.order) for asset in data.assets],
            [(asset.name, asset.source, asset.order) for asset in expected.assets],
        )
        self.assertEqual(result.usage["route"], "text")
        self.assertEqual(result.usage["pages"], 1)
        self.assertIn("elapsed_ms", result.usage)

    def test_parse_failure_is_tool_failure_with_data(self) -> None:
        path = fixtures.make_text(self.tmp / "旧文档.doc", "legacy")

        result = parse.TOOL.invoke(None, path=path)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_PARSE_FAILED)
        self.assertFalse(result.data.ok)
        self.assertIn("另存为", result.data.detail)
        self.assertEqual(result.error.message, result.data.detail)

    def test_invalid_inputs(self) -> None:
        for kwargs in ({"path": None}, {"path": self.tmp / "a.txt", "filename": 123}):
            with self.subTest(kwargs=kwargs):
                result = parse.TOOL.invoke(None, **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERROR_INVALID_INPUT)

    def test_internal_exception_becomes_tool_error(self) -> None:
        path = fixtures.make_text(self.tmp / "a.txt", "x")
        with mock.patch.object(parse.parse_service, "parse_file", side_effect=RuntimeError("boom")):
            result = parse.TOOL.invoke(None, path=path)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INTERNAL)
        self.assertIn("RuntimeError", result.error.message)

    def test_health(self) -> None:
        health = parse.TOOL.health()
        self.assertIsInstance(health, Health)
        self.assertTrue(health.ok)


class OcrToolTests(TempDirCase):
    def test_disabled_ocr_matches_run_ocr(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        settings = fixtures.settings_with()

        expected = ocr_service.run_ocr(path, "图.png", settings=settings)
        result = ocr.TOOL.invoke(ToolContext(settings=settings), path=path, filename="图.png")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_OCR_FAILED)
        self.assertFalse(result.data.ok)
        self.assertEqual(result.data.detail, expected.detail)
        self.assertEqual(result.data.provider, expected.provider)
        self.assertIn("未启用", result.error.message)

    def test_stub_ocr_matches_run_ocr(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        settings = fixtures.settings_with(ocr_mode="manual", ocr_provider="stub")

        expected = ocr_service.run_ocr(path, "图.png", settings=settings)
        result = ocr.TOOL.invoke(ToolContext(settings=settings), path=path, filename="图.png")

        self.assertTrue(result.ok)
        data = result.data
        for field in (
            "ok",
            "text",
            "pages",
            "total_pages",
            "images",
            "job_id",
            "model",
            "provider",
            "detail",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(data, field), getattr(expected, field))
        self.assertEqual(result.usage["provider"], "stub")
        self.assertIn("elapsed_ms", result.usage)

    def test_invalid_inputs(self) -> None:
        for kwargs in ({"path": None}, {"path": self.tmp / "a.png", "filename": 123}):
            with self.subTest(kwargs=kwargs):
                result = ocr.TOOL.invoke(ToolContext(), **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERROR_INVALID_INPUT)

    def test_internal_exception_becomes_tool_error(self) -> None:
        path = fixtures.make_png(self.tmp / "图.png")
        with mock.patch.object(ocr.ocr_service, "run_ocr", side_effect=RuntimeError("boom")):
            result = ocr.TOOL.invoke(ToolContext(), path=path)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INTERNAL)
        self.assertIn("RuntimeError", result.error.message)

    def test_health_follows_config(self) -> None:
        self.assertFalse(ocr.TOOL.health(ToolContext(settings=fixtures.settings_with())).ok)
        enabled = ocr.TOOL.health(
            ToolContext(settings=fixtures.settings_with(ocr_mode="manual", ocr_provider="stub"))
        )
        self.assertTrue(enabled.ok)
        self.assertIn("stub", enabled.detail)

    def test_spec_marks_external_risk(self) -> None:
        self.assertTrue(ocr.TOOL.spec.external)
        self.assertEqual(ocr.TOOL.spec.side_effect, "read_local")
        self.assertEqual(ocr.TOOL.spec.risk, "medium")
        self.assertFalse(ocr.TOOL.spec.allowed_for_skills)


class KnowledgeToolTests(unittest.TestCase):
    def test_search_returns_raw_payload_and_evidence(self) -> None:
        with mock.patch.object(lightrag, "retrieve", return_value=PAYLOAD) as retrieve:
            result = knowledge.TOOL.invoke(None, query="测试查询")

        self.assertTrue(result.ok)
        self.assertIs(result.data, PAYLOAD)
        retrieve.assert_called_once_with(
            "测试查询", top_k=None, chunk_top_k=None, mode="mix"
        )
        self.assertEqual(result.usage["mode"], "mix")
        self.assertIn("elapsed_ms", result.usage)
        self.assertEqual(
            [item.evidence_id for item in result.evidence],
            [
                "entity:项目A",
                "relationship:项目A->项目B",
                "chunk:c1",
                "reference:r1",
            ],
        )

    def test_invalid_inputs_do_not_call_upstream(self) -> None:
        cases = (
            {"query": "ab"},
            {"query": 123},
            {"query": "测试查询", "top_k": 0},
            {"query": "测试查询", "top_k": True},
            {"query": "测试查询", "chunk_top_k": -1},
            {"query": "测试查询", "mode": "bad"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with mock.patch.object(lightrag, "retrieve") as retrieve:
                    result = knowledge.TOOL.invoke(None, **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, ERROR_INVALID_INPUT)
                retrieve.assert_not_called()

    def test_upstream_error_mapping(self) -> None:
        error = UpstreamError(
            "知识库服务不可用",
            upstream_status=503,
            upstream_body="gateway down",
        )
        with mock.patch.object(lightrag, "retrieve", side_effect=error):
            result = knowledge.TOOL.invoke(None, query="测试查询")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_UPSTREAM)
        self.assertTrue(result.error.retryable)
        self.assertEqual(result.error.upstream_status, 503)
        self.assertEqual(result.error.detail, "gateway down")

    def test_timeout_is_retryable(self) -> None:
        error = UpstreamError("知识库查询超时", kind=UPSTREAM_TIMEOUT, status_code=504)
        with mock.patch.object(lightrag, "retrieve", side_effect=error):
            result = knowledge.TOOL.invoke(None, query="测试查询")

        self.assertTrue(result.error.retryable)

    def test_unexpected_exception_is_internal_error(self) -> None:
        with mock.patch.object(lightrag, "retrieve", side_effect=RuntimeError("boom")):
            result = knowledge.TOOL.invoke(None, query="测试查询")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ERROR_INTERNAL)
        self.assertIn("RuntimeError", result.error.message)

    def test_health_ok(self) -> None:
        probe = {"ok": True, "status_code": 200, "detail": None}
        with mock.patch.object(lightrag, "probe_health", return_value=probe):
            health = knowledge.TOOL.health()
        self.assertTrue(health.ok)
        self.assertEqual(health.detail, "HTTP 200")

    def test_health_failure(self) -> None:
        probe = {"ok": False, "status_code": 503, "detail": "down"}
        with mock.patch.object(lightrag, "probe_health", return_value=probe):
            health = knowledge.TOOL.health()
        self.assertFalse(health.ok)
        self.assertEqual(health.detail, "down")

    def test_health_exception_is_reported(self) -> None:
        with mock.patch.object(lightrag, "probe_health", side_effect=RuntimeError("boom")):
            health = knowledge.TOOL.health()
        self.assertFalse(health.ok)
        self.assertIn("探活异常", health.detail)

    def test_spec_is_read_only_remote(self) -> None:
        self.assertEqual(knowledge.TOOL.spec.side_effect, "none")
        self.assertTrue(knowledge.TOOL.spec.external)
        self.assertTrue(knowledge.TOOL.spec.allowed_for_skills)


class EvidenceTests(unittest.TestCase):
    def test_maps_four_kinds(self) -> None:
        evidence = knowledge.evidence_from_query_data(PAYLOAD)
        by_id = {item.evidence_id: item for item in evidence}

        entity = by_id["entity:项目A"]
        self.assertEqual(entity.kind, "entity")
        self.assertEqual(entity.source_id, "项目A")
        self.assertEqual(entity.doc_name, "资料.md")
        self.assertEqual(entity.text, "实体描述")
        self.assertEqual(entity.metadata["entity_type"], "项目")
        self.assertEqual(entity.score, 0.9)

        relation = by_id["relationship:项目A->项目B"]
        self.assertEqual(relation.kind, "relationship")
        self.assertEqual(relation.source_id, "项目A->项目B")
        self.assertEqual(relation.text, "关系描述")
        self.assertEqual(relation.metadata["keywords"], "合作")
        self.assertEqual(relation.score, 0.5)

        chunk = by_id["chunk:c1"]
        self.assertEqual(chunk.kind, "chunk")
        self.assertEqual(chunk.source_id, "c1")
        self.assertEqual(chunk.text, "片段正文")
        self.assertEqual(chunk.metadata["reference_id"], "r1")

        reference = by_id["reference:r1"]
        self.assertEqual(reference.kind, "reference")
        self.assertEqual(reference.source_id, "r1")
        self.assertEqual(reference.text, "引用正文")

    def test_deduplicates_within_kind(self) -> None:
        payload = {
            "data": {
                "entities": [
                    {"entity_name": "重复", "description": "一"},
                    {"entity_name": "重复", "description": "二"},
                ],
                "chunks": [
                    {"chunk_id": "c1", "content": "一"},
                    {"chunk_id": "c1", "content": "二"},
                ],
            }
        }
        evidence = knowledge.evidence_from_query_data(payload)
        self.assertEqual([item.evidence_id for item in evidence], ["entity:重复", "chunk:c1"])

    def test_malformed_payloads_return_empty(self) -> None:
        for payload in (None, {}, {"data": None}, "not-a-dict", {"data": {"chunks": "abc"}}):
            with self.subTest(payload=payload):
                self.assertEqual(knowledge.evidence_from_query_data(payload), [])

    def test_chunk_without_ids_uses_index(self) -> None:
        payload = {"data": {"chunks": [{"content": "无 id"}]}}
        evidence = knowledge.evidence_from_query_data(payload)
        self.assertEqual([item.evidence_id for item in evidence], ["chunk:0"])
        self.assertEqual(evidence[0].source_id, "")


if __name__ == "__main__":
    unittest.main()
