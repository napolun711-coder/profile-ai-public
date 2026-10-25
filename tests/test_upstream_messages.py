"""上游报错翻译单测（Phase 15.1）。

用户看到的失败原因必须是「发生了什么 + 下一步怎么做」，而不是 `HTTP 409` 四个字符。
`errors.humanize_upstream()` 是**纯函数**（不发请求、不读磁盘），所以这里直接比对输出。

样本取自 logs/app.log（2026-10-16 那次重复上传）：LightRAG 对同名文件返回 409，
响应体里的 `detail` 说明了「storage already contains …」，但改造前我们把它丢了。
"""

from __future__ import annotations

import unittest

from app.core import errors

#: LightRAG 拒绝重复上传时的真实响应体结构（原文照抄当天的 409 返回）
DUPLICATE_BODY = (
    '{"detail":"Document storage already contains '
    "'OpenClaw_对接宿主机_Ollama_配置笔记一__环境参数与.md' (Status: processed). "
    'Delete the existing record before re-uploading."}'
)


class HumanizeUpstreamTests(unittest.TestCase):
    """把知识库的拒绝翻译成人话。"""

    def test_duplicate_upload_names_the_document_and_next_step(self) -> None:
        text = errors.humanize_upstream(409, DUPLICATE_BODY)

        self.assertIn("已经有同名文档", text)
        self.assertIn("OpenClaw_对接宿主机_Ollama_配置笔记一__环境参数与.md", text)
        self.assertIn("processed", text)
        self.assertIn("没有写入新文档", text)
        self.assertIn("重试", text)
        self.assertIn("跳过", text)
        # 原文不能丢：用户要能看到知识库到底说了什么
        self.assertIn("already contains", text)

    def test_duplicate_without_json_is_still_explained(self) -> None:
        text = errors.humanize_upstream(409, "Document already exists: 工作总结.md")

        self.assertIn("同名文档", text)
        self.assertNotIn("HTTP 409）", text)   # 不要又退回成「只报状态码」

    def test_plain_text_body_is_flattened(self) -> None:
        text = errors.humanize_upstream(500, "line1\nline2   line3")

        self.assertIn("知识库服务内部错误（HTTP 500）", text)
        self.assertIn("line1 line2 line3", text)
        self.assertNotIn("\n", text)

    def test_known_status_codes_have_actionable_text(self) -> None:
        cases = {
            413: "文件太大",
            415: "不支持这种文件类型",
            429: "太忙",
            401: "鉴权",
            403: "鉴权",
        }
        for status, expected in cases.items():
            text = errors.humanize_upstream(status, "")
            self.assertIn(expected, text, status)
            self.assertIn(str(status), text, status)

    def test_unknown_status_falls_back_to_plain_sentence(self) -> None:
        self.assertEqual(
            errors.humanize_upstream(418, ""), "知识库拒绝了这次提交（HTTP 418）。"
        )

    def test_missing_status_is_reported_as_no_answer(self) -> None:
        self.assertIn("没有正常应答", errors.humanize_upstream(0, ""))
        self.assertIn("没有正常应答", errors.humanize_upstream("", ""))

    def test_long_upstream_text_is_truncated(self) -> None:
        text = errors.humanize_upstream(500, "x" * 1000)

        self.assertIn("…", text)
        self.assertLess(len(text), 250)

    def test_upstream_message_prefers_readable_fields(self) -> None:
        self.assertEqual(errors.upstream_message('{"detail":"坏了"}'), "坏了")
        self.assertEqual(errors.upstream_message('{"message":"坏了"}'), "坏了")
        self.assertEqual(errors.upstream_message(""), "")
        # 结构化但不含可读字段：压成一行也要有内容（不能返回空）
        self.assertTrue(errors.upstream_message('{"other":[1,2]}'))


if __name__ == "__main__":
    unittest.main()
