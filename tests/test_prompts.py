"""P3A：Prompt 注册表单测（纯函数、确定性）。"""

from __future__ import annotations

import unittest

from app.services import prompts


class LayerTests(unittest.TestCase):
    def test_layer_order_is_l0_to_l3(self) -> None:
        self.assertEqual([layer.layer_id for layer in prompts.LAYERS], ["L0", "L1", "L2", "L3"])

    def test_system_prompt_contains_layers_in_order(self) -> None:
        prompt = prompts.build_system_prompt()
        positions = [prompt.index(layer.text) for layer in prompts.LAYERS]

        self.assertEqual(positions, sorted(positions))
        self.assertIn("引用", prompt)
        self.assertIn("[编号]", prompt)
        self.assertIn("每个来自资料的要点", prompt)

    def test_system_prompt_is_deterministic(self) -> None:
        self.assertEqual(prompts.build_system_prompt(), prompts.build_system_prompt())

    def test_output_format_is_appended_last(self) -> None:
        prompt = prompts.build_system_prompt(output_format="每条依据单独一行")

        self.assertTrue(prompt.endswith("输出格式要求：\n每条依据单独一行"))
        self.assertGreater(prompt.index("每条依据单独一行"), prompt.index(prompts.LAYERS[-1].text))

    def test_layer_map(self) -> None:
        mapping = prompts.layer_map()

        self.assertEqual(sorted(mapping), ["L0", "L1", "L2", "L3"])
        self.assertEqual(mapping["L0"].title, "身份")


class MessageTests(unittest.TestCase):
    def test_messages_split_system_and_user(self) -> None:
        messages = prompts.build_messages(
            question="知识库里有哪些项目？",
            context_text="## 资料片段\n[1] a.md\n第一段",
        )

        self.assertEqual([item["role"] for item in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], prompts.build_system_prompt())
        self.assertIn("第一段", messages[1]["content"])
        self.assertIn("问题：知识库里有哪些项目？", messages[1]["content"])

    def test_context_does_not_leak_into_system_prompt(self) -> None:
        secret = "EVIDENCE-SECRET-123"
        messages = prompts.build_messages(question="问题", context_text=secret)

        self.assertNotIn(secret, messages[0]["content"])
        self.assertIn(secret, messages[1]["content"])

    def test_empty_context_is_explicit(self) -> None:
        messages = prompts.build_messages(question="问题", context_text="")

        self.assertIn("没有可用资料", messages[1]["content"])
        self.assertIn("问题：问题", messages[1]["content"])

    def test_empty_question_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            prompts.build_messages(question="   ")

    def test_system_prompt_override(self) -> None:
        messages = prompts.build_messages(
            question="问题",
            context_text="资料",
            system_prompt="自定义系统提示",
        )

        self.assertEqual(messages[0]["content"], "自定义系统提示")

    def test_empty_system_prompt_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            prompts.build_messages(question="问题", system_prompt="   ")


if __name__ == "__main__":
    unittest.main()
