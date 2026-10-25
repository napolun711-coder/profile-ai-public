"""P1：迷你 JSON Schema 子集校验器（不引入 jsonschema 依赖）。

为什么自己写：
    项目运行环境里没有 `jsonschema`，而 P1 只需要校验工具参数/结果的外形，
    不需要完整的 JSON Schema 语义。这里的取舍是**宁可少校验，也不把可用请求判死**：
    不认识的关键字（`$ref`、`oneOf`、`format` 等）一律忽略。

支持的写法：
- `type`：object / array / string / integer / number / boolean / null（可为列表）
- `properties`、`required`、`additionalProperties: false`
- `items`（对象 schema）、`minItems`、`maxItems`
- `enum`
- `minimum`、`maximum`
- `minLength`、`maxLength`、`pattern`

空 schema（`{}` / `None` / 非对象）视为“不校验”。校验器是纯函数：
不抛异常、不读文件、不联网。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

#: 递归深度上限：防御畸形 schema 造成的无限递归（正常 schema 远小于此值）
_MAX_DEPTH = 32

_TYPE_NAMES = {
    "object": "对象",
    "array": "数组",
    "string": "字符串",
    "integer": "整数",
    "number": "数字",
    "boolean": "布尔值",
    "null": "空值",
}


def _is_type(value: Any, name: str) -> Optional[bool]:
    """判断值是否符合某个 JSON 类型；不认识类型名返回 None（忽略该关键字）。"""
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return None


def _same(left: Any, right: Any) -> bool:
    """枚举值比较：JSON 里 true 与 1 是两回事，Python 里却相等，这里对齐 JSON。"""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return left == right


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}"


def check(schema: Any, value: Any, *, path: str = "$", _depth: int = 0) -> List[str]:
    """按 schema 校验 value，返回问题列表；空列表表示通过。

    `path` 只用于拼接可读的问题位置（如 `$.data.entities[0]`），不参与校验。
    """
    if not isinstance(schema, dict) or not schema:
        return []
    if _depth >= _MAX_DEPTH:
        return [f"{path} 的 schema 嵌套过深，已跳过校验"]

    problems: List[str] = []

    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        matched = _is_type(value, raw_type)
        if matched is False:
            readable = _TYPE_NAMES.get(raw_type, raw_type)
            problems.append(f"{path} 应为{readable}，实际是 {type(value).__name__}")
            return problems
    elif isinstance(raw_type, list):
        checked = [_is_type(value, item) for item in raw_type if isinstance(item, str)]
        known = [item for item in checked if item is not None]
        if known and not any(known):
            expected = "/".join(
                _TYPE_NAMES.get(item, item) for item in raw_type if isinstance(item, str)
            )
            problems.append(f"{path} 应为 {expected}，实际是 {type(value).__name__}")
            return problems

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        if not any(_same(value, item) for item in enum):
            problems.append(f"{path} 必须是 {enum} 之一")

    if isinstance(value, dict):
        problems.extend(_check_object(schema, value, path, _depth))
    elif isinstance(value, list):
        problems.extend(_check_array(schema, value, path, _depth))
    elif isinstance(value, str):
        problems.extend(_check_string(schema, value, path))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        problems.extend(_check_number(schema, value, path))

    return problems


def _check_object(
    schema: dict, value: dict, path: str, depth: int
) -> List[str]:
    problems: List[str] = []

    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in value:
                problems.append(f"{path}.{key} 缺少必填字段")

    properties = schema.get("properties")
    property_map = properties if isinstance(properties, dict) else {}

    for key, item_schema in property_map.items():
        if key in value:
            problems.extend(check(item_schema, value[key], path=_join(path, key), _depth=depth + 1))

    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in property_map:
                problems.append(f"{path}.{key} 不是允许的参数")

    return problems


def _check_array(schema: dict, value: list, path: str, depth: int) -> List[str]:
    problems: List[str] = []

    min_items = schema.get("minItems")
    if isinstance(min_items, int) and not isinstance(min_items, bool) and len(value) < min_items:
        problems.append(f"{path} 至少需要 {min_items} 项")

    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and not isinstance(max_items, bool) and len(value) > max_items:
        problems.append(f"{path} 最多允许 {max_items} 项")

    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(value):
            problems.extend(
                check(item_schema, item, path=_join(path, index), _depth=depth + 1)
            )
    return problems


def _check_string(schema: dict, value: str, path: str) -> List[str]:
    problems: List[str] = []

    min_length = schema.get("minLength")
    if isinstance(min_length, int) and not isinstance(min_length, bool) and len(value) < min_length:
        problems.append(f"{path} 长度不能少于 {min_length}")

    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and not isinstance(max_length, bool) and len(value) > max_length:
        problems.append(f"{path} 长度不能超过 {max_length}")

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern:
        try:
            if re.search(pattern, value) is None:
                problems.append(f"{path} 不符合格式 {pattern}")
        except re.error:
            pass  # 畸形正则：忽略，不阻塞调用

    return problems


def _check_number(schema: dict, value: float, path: str) -> List[str]:
    problems: List[str] = []

    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and value < minimum:
        problems.append(f"{path} 不能小于 {minimum}")

    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and value > maximum:
        problems.append(f"{path} 不能大于 {maximum}")

    return problems


__all__ = ["check"]
