"""Phase 18：质检「硬门」不变量的回归测试。

背景：质检曾经有一个总开关（.env 的 QUALITY_ENABLED / 设置页的「质量校验开关」）。
一旦被它关掉，``quality.evaluate()`` 会直接返回 pass —— 乱码、空壳、扫描件都会进知识库，
而且**不留任何痕迹**。Phase 18 起质检是硬门：数据结构、配置、接口、页面里都不存在"关掉它"的入口；
没通过的文件只能由用户在上传卡片上点「仍要入库」事后放行（会写 forced 痕迹）。

这个文件把上面的约束变成可执行的用例（纯离线、不联网、不依赖 .env）：

1. 数据结构：``QualityOptions`` 没有 ``enabled`` 字段（想关也关不掉）；
2. 源码结构：``app/services/quality.py`` 不再访问任何"总开关"属性（AST 扫描，防偷偷加回来）；
3. 行为：settings.quality_enabled=False（旧 .env 残留）时，阈值与结论**完全不变**；
4. 文案：建议（advice）里不可能再出现「质量门已关闭」这类误导说法；
5. 接口：/api/settings 的 quality_effective.enabled 恒为 True，并把被忽略的开关如实告诉页面。
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import unittest
from pathlib import Path
from unittest import mock

from app.core import user_settings
from app.routers import settings as settings_router
from app.services import advice as advice_service
from app.services import quality as q
from tests import fixtures

BASE_DIR = Path(__file__).resolve().parents[1]
QUALITY_MODULE = BASE_DIR / "app" / "services" / "quality.py"
ADVICE_MODULE = BASE_DIR / "app" / "services" / "advice.py"

# 「关掉质检」这件事不该存在的字段名（旧版是 QualityOptions.enabled）
FORBIDDEN_FIELD = "enabled"
FORBIDDEN_WORDING = "质量门已关闭"


class StructureTests(unittest.TestCase):
    """"关掉质检"在数据结构与源码里都不该存在。"""

    def test_options_has_no_enabled_field(self) -> None:
        names = [item.name for item in dataclasses.fields(q.QualityOptions)]

        self.assertNotIn(FORBIDDEN_FIELD, names)
        # 阈值本身一个都不能少（要改这个清单就必须显式改这一行）
        self.assertEqual(
            names,
            [
                "min_chars",
                "min_readable_ratio",
                "max_garbled_ratio",
                "max_repeat_ratio",
                "min_chars_per_page",
                "pass_score",
            ],
        )

    def test_quality_module_never_reads_a_master_switch(self) -> None:
        """AST 扫描：quality.py 里不允许出现 ``.enabled`` 属性访问。"""
        tree = ast.parse(QUALITY_MODULE.read_text(encoding="utf-8"), str(QUALITY_MODULE))
        offenders = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == FORBIDDEN_FIELD
        ]

        self.assertEqual(offenders, [])

    def test_quality_module_has_no_disabled_verdict(self) -> None:
        """源码里不该再有"因为开关关掉所以放行"的字样与调用写法。"""
        source = QUALITY_MODULE.read_text(encoding="utf-8")

        self.assertNotIn(FORBIDDEN_WORDING, source)
        self.assertNotIn("QualityOptions(enabled", source)

    def test_advice_module_has_no_master_switch(self) -> None:
        source = ADVICE_MODULE.read_text(encoding="utf-8")

        self.assertNotIn(FORBIDDEN_WORDING, source)
        self.assertNotIn("quality_enabled", source)

    def test_advice_signature_drops_master_switch(self) -> None:
        params = inspect.signature(advice_service.advise).parameters

        self.assertNotIn("quality_enabled", params)


class BehaviourTests(unittest.TestCase):
    """行为：旧开关不改变任何结论；没通过的内容一律拦下。"""

    SHORT_TEXT = "只有几个字"

    def test_ignored_switch_keeps_thresholds_and_verdict(self) -> None:
        on = q.options_from_settings(fixtures.settings_with(quality_enabled=True))
        off = q.options_from_settings(fixtures.settings_with(quality_enabled=False))

        # 阈值完全一样（连预设对象都是同一个）
        self.assertEqual(on, off)
        on_report = q.evaluate(self.SHORT_TEXT, route="text", pages=1, options=on)
        off_report = q.evaluate(self.SHORT_TEXT, route="text", pages=1, options=off)
        self.assertEqual(on_report.level, off_report.level)
        self.assertEqual(off_report.level, q.LEVEL_BLOCKED)

    def test_garbled_text_is_always_blocked(self) -> None:
        garbled = "\ufffd" * 500

        for flag in (True, False):
            with self.subTest(quality_enabled=flag):
                options = q.options_from_settings(fixtures.settings_with(quality_enabled=flag))
                report = q.evaluate(garbled, route="text", pages=1, options=options)
                self.assertEqual(report.level, q.LEVEL_BLOCKED)

    def test_custom_profile_still_honours_thresholds(self) -> None:
        options = q.options_from_settings(
            fixtures.settings_with(
                quality_profile="custom", quality_min_chars=5000, quality_enabled=False
            )
        )

        self.assertEqual(options.min_chars, 5000)
        self.assertEqual(
            q.evaluate(self.SHORT_TEXT, route="text", pages=1, options=options).level,
            q.LEVEL_BLOCKED,
        )


class AdviceTests(unittest.TestCase):
    """建议文案：不许再出现"质检已关闭"这种说法。"""

    def test_advice_has_no_disabled_wording(self) -> None:
        result = advice_service.advise("太短的一段话")

        self.assertFalse([item for item in result.checks if "已关闭" in item["detail"]])
        content = next(item for item in result.checks if item["name"] == "内容量")
        self.assertFalse(content["ok"])   # 内容太少 → 如实提示，不再"放行式安慰"


class SettingsPayloadTests(unittest.TestCase):
    """接口：质检恒为开启，并如实说明"你关过，但被忽略了"。"""

    def _payload(self, **overrides):
        with mock.patch.object(
            settings_router, "get_settings", return_value=fixtures.settings_with(**overrides)
        ), mock.patch.object(
            settings_router.user_settings, "load", return_value=copy.deepcopy(user_settings.DEFAULTS)
        ):
            return settings_router.read_settings()

    def test_effective_always_on(self) -> None:
        quality = self._payload()["quality_effective"]

        self.assertIs(quality["enabled"], True)
        self.assertFalse(quality["switch_ignored"])

    def test_ignored_switch_is_reported(self) -> None:
        quality = self._payload(quality_enabled=False)["quality_effective"]

        # 页面要能说清"你在 .env 里关过，但它被忽略了"，而不是默默不生效
        self.assertIs(quality["enabled"], True)
        self.assertTrue(quality["switch_ignored"])


if __name__ == "__main__":
    unittest.main()
