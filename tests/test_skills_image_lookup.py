"""P2C：`image_lookup` 技能的离线单测。

用假 Context / 假 ToolManager 替代真实工具：不联网、不写审计、不碰真实索引。
覆盖规则路由、两通道合并、partial/error、引用映射与 trace 脱敏。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from app.plugins import Evidence, ToolError, ToolResult
from app.services import image_evidence
from app.skills import SkillInput, SkillManager, SkillRegistry
from app.skills.builtin import BUILTIN_SKILLS, image_lookup, knowledge_qa
from app.skills.builtin import register_builtin_skills


def image_item(
    file_id: str = "f1",
    doc_name: str = "书桌照片_abcdef.md",
    *,
    text: str = "OCR 预览正文",
    caption: str = "描述正文",
    score: float = 1.0,
    channels=("gallery",),
) -> Evidence:
    image_id = f"img:{file_id}:0"
    return Evidence(
        evidence_id=f"image:{image_id}",
        kind="image",
        source_id=image_id,
        doc_name=doc_name,
        file_id=file_id,
        image_id=image_id,
        text=text,
        origin_url=f"/api/files/raw?file_id={file_id}",
        score=score,
        metadata={
            "gallery": {
                "kind": "image",
                "caption": caption,
                "ocr_preview": text,
                "asset_paths": ["source.jpg"],
                "keywords": ["书桌照片"],
            },
            "channels": list(channels),
        },
    )


def chunk_item(
    file_id: str = "f2",
    doc_name: str = "窗外照片_123456.md",
    *,
    text: str = "检索到的正文",
    source_id: str = "c1",
    score: float = 0.7,
) -> Evidence:
    return Evidence(
        evidence_id=f"chunk:{source_id}",
        kind="chunk",
        source_id=source_id,
        doc_name=doc_name,
        text=text,
        score=score,
        metadata={"reference_id": "3"},
    )


def gallery_mapping(file_id: str = "f2", doc_name: str = "窗外照片_123456.md"):
    items = {
        doc_name: {
            "doc_name": doc_name,
            "file_id": file_id,
            "evidence": {
                "kind": "image",
                "image_id": f"img:{file_id}:0",
                "caption": "窗外描述",
                "ocr_preview": "窗外预览",
                "asset_paths": ["source.jpg"],
                "keywords": ["窗外照片"],
            },
        }
    }
    return image_evidence.image_map(items)


class FakeTrace:
    def __init__(self) -> None:
        self.detail = {}


class FakeContext:
    def __init__(self, results) -> None:
        self.results = results
        self.calls = []
        self.trace = FakeTrace()

    def call_tool(self, name, *, args=None, tool_ctx=None):
        self.calls.append({"name": name, "args": args})
        return self.results[name]


def success(evidence=None, data=None) -> ToolResult:
    return ToolResult.success(data or {}, evidence=list(evidence or []))


def failure(code: str = "upstream_error", message: str = "检索失败") -> ToolResult:
    return ToolResult.failure(ToolError(code=code, message=message, retryable=True))


class RegistryBoundaryTests(unittest.TestCase):
    def test_image_lookup_is_opt_in(self) -> None:
        names = [skill.spec.name for skill in BUILTIN_SKILLS]
        self.assertEqual(names, ["knowledge_qa"])
        self.assertEqual(register_builtin_skills().names(), ["knowledge_qa"])


class CanHandleTests(unittest.TestCase):
    def test_empty_scores_zero(self) -> None:
        self.assertEqual(image_lookup.SKILL.can_handle(SkillInput("   ")), 0.0)

    def test_image_cue_scores_high(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(SkillInput("找一下书桌照片")),
            image_lookup.IMAGE_SCORE,
        )

    def test_knowledge_only_scores_zero(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(SkillInput("知识库里有哪些项目")),
            0.0,
        )

    def test_image_and_knowledge_cue_still_image(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(SkillInput("知识库里的这张图讲了什么")),
            image_lookup.IMAGE_SCORE,
        )

    def test_image_with_document_cue_yields_to_knowledge(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(
                SkillInput("知识库里的图片和文档分别有哪些？")
            ),
            image_lookup.MIXED_IMAGE_SCORE,
        )

    def test_image_recognition_cue_yields_to_knowledge(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(
                SkillInput("项目里有没有关于图片识别的说明？")
            ),
            image_lookup.MIXED_IMAGE_SCORE,
        )

    def test_knowledge_base_location_stays_image(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(SkillInput("找一下知识库里的图片")),
            image_lookup.IMAGE_SCORE,
        )

    def test_non_string_scores_zero(self) -> None:
        self.assertEqual(
            image_lookup.SKILL.can_handle(SimpleNamespace(question=None)), 0.0
        )


class ExecuteTests(unittest.TestCase):
    def test_two_channels_merge_and_cite(self) -> None:
        local = image_item("f1", "书桌照片_abcdef.md")
        retrieval_chunk = chunk_item()
        ctx = FakeContext(
            {
                "image.search": success([local]),
                "knowledge.search": success([retrieval_chunk]),
            }
        )

        with mock.patch.object(
            image_evidence, "image_map", return_value=gallery_mapping()
        ):
            output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(output.status, "ok")
        self.assertEqual(len(output.evidence), 2)
        self.assertEqual([c.label for c in output.citations], ["[img-1]", "[img-2]"])
        self.assertIn("找到 2 张相关图片", output.answer)
        self.assertIn("书桌照片_abcdef.md", output.answer)
        self.assertEqual(output.usage["local_count"], 1)
        self.assertEqual(output.usage["retrieval_count"], 1)
        self.assertEqual(output.usage["image_count"], 2)
        self.assertEqual(output.usage["citation_count"], 2)
        self.assertEqual(
            ctx.calls,
            [
                {
                    "name": "image.search",
                    "args": {"query": "找一下书桌照片", "limit": 8},
                },
                {
                    "name": "knowledge.search",
                    "args": {"query": "找一下书桌照片", "chunk_top_k": 8},
                },
            ],
        )
        self.assertTrue(ctx.trace.detail["local_ok"])
        self.assertTrue(ctx.trace.detail["retrieval_ok"])
        self.assertFalse(ctx.trace.detail["no_images"])

    def test_local_only_when_retrieval_fails(self) -> None:
        ctx = FakeContext(
            {
                "image.search": success([image_item()]),
                "knowledge.search": failure("upstream_error", "知识库不可用"),
            }
        )

        output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(output.status, "partial")
        self.assertEqual(len(output.evidence), 1)
        self.assertEqual(output.error, None)
        self.assertFalse(output.usage["retrieval_ok"])
        self.assertEqual(ctx.trace.detail["retrieval_error"], "upstream_error")
        self.assertTrue(ctx.trace.detail["local_ok"])

    def test_retrieval_only_when_local_fails(self) -> None:
        ctx = FakeContext(
            {
                "image.search": failure("internal_error", "本地不可用"),
                "knowledge.search": success([chunk_item()]),
            }
        )

        with mock.patch.object(
            image_evidence, "image_map", return_value=gallery_mapping()
        ):
            output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(output.status, "partial")
        self.assertEqual(len(output.evidence), 1)
        self.assertEqual(output.evidence[0].file_id, "f2")
        self.assertFalse(output.usage["local_ok"])
        self.assertEqual(ctx.trace.detail["local_error"], "internal_error")

    def test_both_channels_failing_is_error(self) -> None:
        ctx = FakeContext(
            {
                "image.search": failure("internal_error", "本地不可用"),
                "knowledge.search": failure("upstream_error", "知识库不可用"),
            }
        )

        output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(output.status, "error")
        self.assertEqual(output.error.code, "internal_error")
        self.assertEqual(ctx.trace.detail["local_error"], "internal_error")
        self.assertEqual(ctx.trace.detail["retrieval_error"], "upstream_error")
        rendered = json.dumps(output.to_dict(), ensure_ascii=False)
        self.assertNotIn("detail", rendered)

    def test_no_images_is_partial(self) -> None:
        ctx = FakeContext(
            {
                "image.search": success([]),
                "knowledge.search": success([]),
            }
        )

        output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(output.status, "partial")
        self.assertEqual(output.evidence, [])
        self.assertEqual(output.citations, [])
        self.assertIn("没有找到", output.answer)
        self.assertTrue(ctx.trace.detail["no_images"])

    def test_duplicate_image_across_channels_is_deduped(self) -> None:
        local = image_item("f1", "书桌照片_abcdef.md")
        retrieval_chunk = chunk_item(
            "f1", "书桌照片_abcdef.md", text="检索正文更长的内容", source_id="c9"
        )
        ctx = FakeContext(
            {
                "image.search": success([local]),
                "knowledge.search": success([retrieval_chunk]),
            }
        )

        with mock.patch.object(
            image_evidence,
            "image_map",
            return_value=gallery_mapping("f1", "书桌照片_abcdef.md"),
        ):
            output = image_lookup.SKILL.execute(SkillInput("找一下书桌照片"), ctx)

        self.assertEqual(len(output.evidence), 1)
        self.assertEqual(len(output.citations), 1)
        self.assertEqual(
            output.evidence[0].metadata["channels"], ["gallery", "retrieval"]
        )
        self.assertEqual(output.evidence[0].text, "检索正文更长的内容")

    def test_trace_and_serialization_never_contain_bodies(self) -> None:
        secret_question = "机密问题正文 书桌照片"
        secret_answer = "机密答案正文"
        secret_caption = "机密描述正文"
        secret_preview = "机密预览正文"
        secret_evidence = "机密证据正文"
        local = image_item(
            "f1",
            "书桌照片_abcdef.md",
            text=secret_preview,
            caption=secret_caption,
        )
        local = replace(local, text=secret_evidence)
        ctx = FakeContext(
            {
                "image.search": success([local]),
                "knowledge.search": success([]),
            }
        )

        output = image_lookup.SKILL.execute(SkillInput(secret_question), ctx)
        output.answer = secret_answer

        rendered_trace = json.dumps(output.trace, ensure_ascii=False)
        rendered_ctx_trace = json.dumps(ctx.trace.detail, ensure_ascii=False)
        rendered_output = json.dumps(output.to_dict(), ensure_ascii=False)

        for label, secret in (
            ("question", secret_question),
            ("answer", secret_answer),
            ("caption", secret_caption),
            ("preview", secret_preview),
            ("evidence", secret_evidence),
        ):
            with self.subTest(label=label):
                self.assertNotIn(secret, rendered_trace)
                self.assertNotIn(secret, rendered_ctx_trace)
                self.assertNotIn(secret, rendered_output)


class CitationTests(unittest.TestCase):
    def test_labels_are_sequential_and_metadata_is_copied(self) -> None:
        first = image_item("f1", "书桌照片_abcdef.md", caption="描述一")
        second = image_item("f2", "窗外照片_123456.md", caption="描述二")

        citations = image_lookup.citations_from_images([first, second, first])

        self.assertEqual([c.label for c in citations], ["[img-1]", "[img-2]"])
        self.assertEqual(citations[0].caption, "描述一")
        self.assertEqual(citations[1].asset_paths, ("source.jpg",))
        self.assertEqual(citations[1].kind, "image")

    def test_non_image_evidence_is_skipped(self) -> None:
        chunk = chunk_item()

        self.assertEqual(image_lookup.citations_from_images([chunk]), [])


class FakeToolManager:
    def __init__(self, results) -> None:
        self.results = results
        self.calls = []

    def call(self, name, *, args=None, ctx=None, caller="", declared=()):
        self.calls.append(
            {"name": name, "args": args, "caller": caller, "declared": declared}
        )
        return self.results[name]


class ManagerIntegrationTests(unittest.TestCase):
    def make_manager(self, tools):
        registry = SkillRegistry()
        registry.register(knowledge_qa.SKILL)
        registry.register(image_lookup.SKILL)
        return SkillManager(registry, tools=tools, fallback="knowledge_qa")

    def test_image_question_routes_to_image_lookup(self) -> None:
        tools = FakeToolManager(
            {
                "image.search": success([image_item()]),
                "knowledge.search": success([]),
            }
        )
        manager = self.make_manager(tools)

        output = manager.run(SkillInput("找一下书桌照片"))

        self.assertEqual(output.trace["route"], "rule")
        self.assertEqual(output.trace["skill"], "image_lookup")
        self.assertEqual(output.trace["steps"], 2)
        self.assertEqual(
            [call["name"] for call in tools.calls],
            ["image.search", "knowledge.search"],
        )
        self.assertEqual(tools.calls[0]["caller"], "skill")
        self.assertEqual(
            tools.calls[0]["declared"],
            ("image.search", "knowledge.search"),
        )

    def test_knowledge_question_stays_on_knowledge_qa(self) -> None:
        tools = FakeToolManager(
            {
                "knowledge.answer": ToolResult.success(
                    {
                        "response": "答案 [1]",
                        "references": [{"reference_id": "1", "file_path": "a.md"}],
                        "response_time": 1.0,
                    },
                    evidence=[
                        Evidence(
                            evidence_id="reference:1",
                            kind="reference",
                            source_id="1",
                            doc_name="a.md",
                        )
                    ],
                )
            }
        )
        manager = self.make_manager(tools)

        output = manager.run(SkillInput("知识库里有哪些项目"))

        self.assertEqual(output.trace["skill"], "knowledge_qa")
        self.assertEqual([call["name"] for call in tools.calls], ["knowledge.answer"])


if __name__ == "__main__":
    unittest.main()
