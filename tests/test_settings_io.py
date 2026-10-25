"""用户配置读写单测（Phase 9）：质量门 / OCR 分组能存、能校验、能注入环境变量。

这些用例**不读取真实的 config/user-settings.json**：需要 load() 的用例统一把配置路径
指向临时文件（见 IsolatedConfigMixin），否则它们会隐式依赖「开发机上的配置存过什么」。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import user_settings


class IsolatedConfigMixin:
    """把 config/user-settings.json 指到临时目录（本机真实配置不参与断言）。"""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        for name in ("SETTINGS_FILE", "BACKUP_FILE"):
            patcher = mock.patch.object(user_settings, name, base / f"{name.lower()}.json")
            patcher.start()
            self.addCleanup(patcher.stop)


class DefaultsTests(IsolatedConfigMixin, unittest.TestCase):
    def test_new_groups_exist_and_default_to_empty(self) -> None:
        """留空 = 不注入（沿用 .env / 内置默认值），避免"打开设置页就把 .env 覆盖掉"。"""
        for group in ("quality", "ocr", "rag"):
            with self.subTest(group=group):
                section = user_settings.DEFAULTS[group]
                self.assertTrue(section)
                self.assertTrue(all(value == "" for value in section.values()))

    def test_defaults_reachable_from_load(self) -> None:
        data = user_settings.load()

        self.assertIn("quality", data)
        self.assertIn("ocr", data)
        self.assertIn("rag", data)
        self.assertEqual(data["ocr"]["token"], "")


class ValidateTests(unittest.TestCase):
    def _merge(self, incoming):
        merged, errors = user_settings.validate_and_merge({}, incoming)
        return merged, errors
    def test_valid_values_are_kept(self) -> None:
        merged, errors = self._merge(
            {
                "quality": {"profile": "strict", "min_chars": "120", "enabled": "true"},
                "ocr": {"mode": "manual", "provider": "local", "image_ocr": "false"},
            }
        )

        self.assertEqual(errors, [])
        self.assertEqual(merged["quality"]["profile"], "strict")
        self.assertEqual(merged["quality"]["min_chars"], "120")
        self.assertEqual(merged["quality"]["enabled"], "true")
        self.assertEqual(merged["ocr"]["mode"], "manual")
        self.assertEqual(merged["ocr"]["provider"], "local")
        self.assertEqual(merged["ocr"]["image_ocr"], "false")

    def test_enum_values_are_case_insensitive(self) -> None:
        merged, errors = self._merge({"ocr": {"mode": "MANUAL", "provider": "PADDLE"}})

        self.assertEqual(errors, [])
        self.assertEqual(merged["ocr"]["mode"], "manual")
        self.assertEqual(merged["ocr"]["provider"], "paddle")

    def test_invalid_profile_is_rejected(self) -> None:
        merged, errors = self._merge({"quality": {"profile": "超严格"}})

        self.assertTrue(any("预设" in message for message in errors))
        self.assertEqual(merged["quality"]["profile"], "")

    def test_invalid_numeric_is_rejected(self) -> None:
        merged, errors = self._merge({"quality": {"min_chars": "五十"}})

        self.assertTrue(any("数字" in message for message in errors))
        self.assertEqual(merged["quality"]["min_chars"], "")

    def test_invalid_mode_and_provider_are_rejected(self) -> None:
        merged, errors = self._merge({"ocr": {"mode": "sometimes", "provider": "openai"}})

        self.assertEqual(len(errors), 2)
        self.assertEqual(merged["ocr"]["mode"], "")
        self.assertEqual(merged["ocr"]["provider"], "")

    def test_empty_token_keeps_existing_value(self) -> None:
        """密钥留空 = 保持现有值不变（避免误清空）。"""
        current = {"ocr": {"token": "secret-token"}}
        merged, errors = user_settings.validate_and_merge(current, {"ocr": {"token": ""}})

        self.assertEqual(errors, [])
        self.assertEqual(merged["ocr"]["token"], "secret-token")


class EnvTests(IsolatedConfigMixin, unittest.TestCase):
    def test_to_env_only_injects_non_empty_app_values(self) -> None:
        data = user_settings.load()
        self.assertEqual(
            user_settings.to_env(data, "app"),
            {"GENERATION_MODE": "upstream"},
        )

        data["ocr"].update({"mode": "manual", "provider": "local", "token": "abc12345"})
        data["quality"].update({"profile": "loose", "max_unocr_images": "3"})
        env = user_settings.to_env(data, "app")

        self.assertEqual(env["OCR_MODE"], "manual")
        self.assertEqual(env["OCR_PROVIDER"], "local")
        self.assertEqual(env["OCR_TOKEN"], "abc12345")
        self.assertEqual(env["QUALITY_PROFILE"], "loose")
        self.assertEqual(env["QUALITY_MAX_UNOCR_IMAGES"], "3")
        self.assertNotIn("QUALITY_MIN_CHARS", env)

    def test_mask_hides_token(self) -> None:
        data = user_settings.load()
        data["ocr"]["token"] = "abcdef123456"

        masked = user_settings.mask(data)

        self.assertEqual(masked["ocr"]["token"], "******3456")

    def test_mask_env_hides_token_like_names(self) -> None:
        masked = user_settings.mask_env({"OCR_TOKEN": "abcdef123456", "OCR_MODE": "manual"})

        self.assertEqual(masked["OCR_TOKEN"], "******3456")
        self.assertEqual(masked["OCR_MODE"], "manual")


class InboxSettingsTests(IsolatedConfigMixin, unittest.TestCase):
    """Phase 16：文件夹轮询的三个网页可配置项（开关 / 间隔 / 收件箱确认门）。"""

    def test_defaults_are_empty_and_synced_with_example(self) -> None:
        section = user_settings.DEFAULTS["inbox"]

        self.assertEqual(sorted(section), ["confirm", "enabled", "poll_seconds"])
        self.assertTrue(all(value == "" for value in section.values()))
        # example 文件必须与 DEFAULTS 逐组对齐（.clinerules 的硬性要求）
        self.assertIn("inbox", self.example_groups())

    def example_groups(self):
        import json

        example = json.loads(
            (user_settings.EXAMPLE_FILE).read_text(encoding="utf-8")
        )
        return set(example)

    def test_valid_values_are_kept_and_injected(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            {},
            {"inbox": {"enabled": "true", "poll_seconds": "15", "confirm": "always"}},
        )

        self.assertEqual(errors, [])
        self.assertEqual(merged["inbox"]["confirm"], "always")
        env = user_settings.to_env(merged, "app")
        self.assertEqual(env["INBOX_ENABLED"], "true")
        self.assertEqual(env["INBOX_POLL_SECONDS"], "15")
        self.assertEqual(env["INBOX_CONFIRM"], "always")

    def test_blank_means_follow_env(self) -> None:
        merged, errors = user_settings.validate_and_merge({}, {"inbox": {}})

        self.assertEqual(errors, [])
        self.assertEqual(user_settings.to_env(merged, "app").get("INBOX_CONFIRM"), None)

    def test_bad_values_are_rejected_with_chinese_reasons(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            {},
            {"inbox": {"enabled": "也许", "poll_seconds": "十秒", "confirm": "sometimes"}},
        )

        self.assertEqual(merged["inbox"]["enabled"], "")
        self.assertEqual(merged["inbox"]["poll_seconds"], "")
        self.assertEqual(merged["inbox"]["confirm"], "")
        self.assertTrue(any("文件夹轮询开关" in item for item in errors))
        self.assertTrue(any("扫描间隔" in item or "文件夹轮询间隔" in item for item in errors))
        self.assertTrue(any("文件夹入库确认方式" in item for item in errors))


class SkillRoutingSettingsTests(IsolatedConfigMixin, unittest.TestCase):
    """P2G：设置页的问答技能路由（留空 = 跟随 .env / 默认 off）。"""

    def test_default_is_empty_and_example_is_synced(self) -> None:
        self.assertEqual(user_settings.DEFAULTS["app"]["skill_routing"], "")

        import json

        example = json.loads(user_settings.EXAMPLE_FILE.read_text(encoding="utf-8"))
        self.assertIn("skill_routing", example["app"])
        self.assertEqual(example["app"]["skill_routing"], "")

    def test_valid_values_are_kept_and_injected(self) -> None:
        for value, expected in (("on", "on"), ("OFF", "off")):
            with self.subTest(value=value):
                merged, errors = user_settings.validate_and_merge(
                    {}, {"app": {"skill_routing": value}}
                )

                self.assertEqual(errors, [])
                self.assertEqual(merged["app"]["skill_routing"], expected)
                self.assertEqual(
                    user_settings.to_env(merged, "app")["SKILL_ROUTING"], expected
                )

    def test_blank_means_follow_env(self) -> None:
        merged, errors = user_settings.validate_and_merge({}, {"app": {}})

        self.assertEqual(errors, [])
        self.assertNotIn("SKILL_ROUTING", user_settings.to_env(merged, "app"))

    def test_bad_value_is_rejected(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            {}, {"app": {"skill_routing": "shadow"}}
        )

        self.assertEqual(merged["app"]["skill_routing"], "")
        self.assertTrue(any("技能路由" in item for item in errors))


class GenerationModeSettingsTests(IsolatedConfigMixin, unittest.TestCase):
    """P3B：设置页的回答生成方式（留空 = 跟随 .env / 默认 upstream）。"""

    def test_default_is_empty_and_example_is_synced(self) -> None:
        self.assertEqual(user_settings.DEFAULTS["app"]["generation_mode"], "upstream")

        import json

        example = json.loads(user_settings.EXAMPLE_FILE.read_text(encoding="utf-8"))
        self.assertIn("generation_mode", example["app"])
        self.assertEqual(example["app"]["generation_mode"], "upstream")

    def test_valid_values_are_kept_and_injected(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            {}, {"app": {"generation_mode": "UPSTREAM"}}
        )
        self.assertEqual(errors, [])
        self.assertEqual(merged["app"]["generation_mode"], "upstream")
        self.assertEqual(
            user_settings.to_env(merged, "app")["GENERATION_MODE"], "upstream"
        )

    def test_blank_means_follow_env(self) -> None:
        merged, errors = user_settings.validate_and_merge({}, {"app": {}})

        self.assertEqual(errors, [])
        self.assertEqual(
            user_settings.to_env(merged, "app")["GENERATION_MODE"], "upstream"
        )

    def test_bad_value_is_rejected(self) -> None:
        merged, errors = user_settings.validate_and_merge(
            {}, {"app": {"generation_mode": "cloud"}}
        )

        self.assertEqual(merged["app"]["generation_mode"], "upstream")
        self.assertTrue(any("回答生成方式" in item for item in errors))


class SkillRoutingPayloadTests(IsolatedConfigMixin, unittest.TestCase):
    def test_app_effective_reports_effective_and_saved_values(self) -> None:
        from app.routers import settings as settings_router

        data = user_settings.load()
        data["app"]["skill_routing"] = "on"
        with mock.patch.object(user_settings, "load", return_value=data), \
                mock.patch.object(settings_router, "_effective", return_value={}):
            payload = settings_router._payload()

        app = payload["app_effective"]
        self.assertIn("skill_routing", app)
        self.assertEqual(app["skill_routing_saved"], "on")
        self.assertIsInstance(app["skill_routing_restart_needed"], bool)
        self.assertIn("generation_mode", app)
        self.assertIn("local_llm_model", app)
        self.assertIn("local_llm_base_url", app)
        self.assertIn("generation_mode_restart_needed", app)


if __name__ == "__main__":
    unittest.main()
