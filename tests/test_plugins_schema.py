"""P1A：迷你 JSON Schema 子集校验器的单测（纯函数，无 I/O）。"""

from __future__ import annotations

import unittest

from app.plugins.schema import check

OBJECT = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 3},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 100},
        "mode": {"type": "string", "enum": ["local", "global"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["query"],
    "additionalProperties": False,
}


class SchemaCheckTests(unittest.TestCase):
    def test_valid_object_passes(self) -> None:
        self.assertEqual(
            check(
                OBJECT,
                {"query": "abc", "top_k": 5, "mode": "local", "tags": ["a", "b"]},
            ),
            [],
        )

    def test_empty_or_malformed_schema_is_not_checked(self) -> None:
        for schema in ({}, None, [], "x", 3):
            with self.subTest(schema=schema):
                self.assertEqual(check(schema, {"anything": object()}), [])

    def test_missing_required_field(self) -> None:
        problems = check(OBJECT, {"top_k": 1})
        self.assertEqual(len(problems), 1)
        self.assertIn("$.query", problems[0])
        self.assertIn("缺少必填字段", problems[0])

    def test_additional_properties_rejected(self) -> None:
        problems = check(OBJECT, {"query": "abc", "extra": 1})
        self.assertEqual(len(problems), 1)
        self.assertIn("$.extra", problems[0])
        self.assertIn("不是允许的参数", problems[0])

    def test_type_checking(self) -> None:
        self.assertTrue(check(OBJECT, {"query": "abc", "top_k": "5"}))
        self.assertTrue(check(OBJECT, {"query": 123}))
        self.assertTrue(check(OBJECT, {"query": "abc", "tags": "x"}))
        self.assertEqual(check(OBJECT, {"query": "abc", "top_k": 1}), [])
        self.assertEqual(check({"type": "number"}, 1), [])
        self.assertEqual(check({"type": "number"}, 1.5), [])
        self.assertTrue(check({"type": "number"}, True))

    def test_bool_is_not_integer_or_number(self) -> None:
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
        }
        problems = check(schema, {"n": True})
        self.assertEqual(len(problems), 1)
        self.assertIn("整数", problems[0])

    def test_numeric_bounds(self) -> None:
        self.assertTrue(check(OBJECT, {"query": "abc", "top_k": 0}))
        self.assertTrue(check(OBJECT, {"query": "abc", "top_k": 101}))
        self.assertEqual(check(OBJECT, {"query": "abc", "top_k": 1}), [])

    def test_string_length_and_enum(self) -> None:
        problems = check(OBJECT, {"query": "ab", "mode": "hybrid"})
        messages = "\n".join(problems)
        self.assertIn("长度不能少于 3", messages)
        self.assertIn("必须是", messages)

    def test_array_items_and_bounds(self) -> None:
        problems = check(OBJECT, {"query": "abc", "tags": ["a", 2, "c"]})
        messages = "\n".join(problems)
        self.assertIn("$.tags[1]", messages)
        self.assertIn("最多允许 2 项", messages)

    def test_enum_does_not_treat_true_as_one(self) -> None:
        schema = {"enum": [1, 2]}
        self.assertTrue(check(schema, True))
        self.assertEqual(check(schema, 1), [])

    def test_type_union(self) -> None:
        schema = {"type": ["string", "null"]}
        self.assertEqual(check(schema, None), [])
        self.assertEqual(check(schema, "x"), [])
        self.assertTrue(check(schema, 1))

    def test_unknown_type_is_ignored(self) -> None:
        self.assertEqual(check({"type": "date-time"}, "whatever"), [])

    def test_invalid_pattern_is_ignored(self) -> None:
        schema = {"type": "string", "pattern": "(["}
        self.assertEqual(check(schema, "abc"), [])

    def test_pattern_mismatch(self) -> None:
        schema = {"type": "string", "pattern": r"^[a-z]+$"}
        self.assertTrue(check(schema, "ABC"))
        self.assertEqual(check(schema, "abc"), [])

    def test_never_raises_on_weird_values(self) -> None:
        weird = [object(), float("nan"), b"bytes", {1: 2}, [1, 2]]
        for value in weird:
            with self.subTest(value=type(value).__name__):
                check(OBJECT, value)


if __name__ == "__main__":
    unittest.main()
