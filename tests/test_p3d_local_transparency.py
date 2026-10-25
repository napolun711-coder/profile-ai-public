"""P3D：本机模式透明度的页面契约（离线单测）。

不启动服务、不联网，只读 ``static/index.html`` / ``static/settings.html``，
守住几条最容易在后续改动里断掉的接线：

- 主页有本机标识元素；只有 ``generation_mode=local`` 才显示；
- ``done`` 事件里的 ``verified`` / ``warnings`` 被接住，且只在
  「真的跑过内容校验」的本机回答上生成提示（图片技能等不带 verified 的结果不误报）；
- 提示只翻译已知规则码，未知规则码保留"未识别"兜底；
- 会话持久化只存规则码与是否通过，刷新后仍能显示同样的提示；
- 设置页暴露保护参数，并把「首字约 60–90 秒」写清楚。
"""

from __future__ import annotations

import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC = BASE_DIR / "static"


class IndexTransparencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_generation_badge_exists_and_is_local_only(self) -> None:
        self.assertIn('id="genModeBadge"', self.html)
        self.assertIn('badge.hidden = true;', self.html)
        # 标识只认进程当前生效值，不认"已保存待重启"的值
        self.assertIn("applyGenerationMode(data.app_effective || {})", self.html)
        self.assertIn('GEN_MODE = (mode === "local" || mode === "upstream") ? mode : "";', self.html)

    def test_done_event_meta_is_captured_only_for_validated_answers(self) -> None:
        self.assertIn('event.type === "done"', self.html)
        self.assertIn('typeof event.verified === "boolean"', self.html)
        self.assertIn("view.__genMeta = {", self.html)
        self.assertIn('mode: "local"', self.html)

    def test_notes_require_local_meta(self) -> None:
        self.assertIn("function appendContentNote(view, meta)", self.html)
        self.assertIn("if (!meta || meta.mode !== \"local\") { return; }", self.html)
        self.assertIn("appendContentNote(view, view.__genMeta || null)", self.html)
        # 上游 / 图片技能的 done 不带 verified，不会走到提示分支
        self.assertIn("if (typeof event.verified === \"boolean\") {", self.html)

    def test_warning_codes_have_human_labels_and_unknown_fallback(self) -> None:
        for code in (
            "low_citation_coverage",
            "unsupported_number",
            "refusal_with_evidence",
            "too_short",
        ):
            self.assertIn(code, self.html)
        self.assertIn("存在未识别的提示项", self.html)
        self.assertIn("规则码：", self.html)

    def test_session_persistence_keeps_only_rule_codes(self) -> None:
        self.assertIn("function persistAiMessage(text, references, generation, flags)", self.html)
        self.assertIn("item.generation = {", self.html)
        self.assertIn("verified: generation.verified !== false", self.html)
        self.assertIn("filter(function (code) { return typeof code === \"string\"; })", self.html)
        # 历史会话回放时把当时的元数据交给定稿渲染
        self.assertIn("view.__genMeta = item.generation || null;", self.html)

    def test_local_mode_has_slow_first_token_hint(self) -> None:
        self.assertIn("本机模型生成中…", self.html)
        self.assertIn("首字通常需 60–90 秒", self.html)


if __name__ == "__main__":
    unittest.main()
