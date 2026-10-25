"""P0：内置工具适配器。

`BUILTIN_TOOLS` 只是对象清单，不注册、不发请求、不读文件。
调用方必须显式 `register_builtins()` / `load_builtin_tools()` 才会进入注册表。
"""

from __future__ import annotations

from typing import Optional

from app.plugins.adapters import knowledge, ocr, parse
from app.plugins.registry import ToolRegistry

BUILTIN_TOOLS = (ocr.TOOL, parse.TOOL, knowledge.TOOL)


def register_builtins(registry: Optional[ToolRegistry] = None) -> ToolRegistry:
    """把内置工具注册进给定注册表；不传则新建一个。"""
    if registry is None:
        registry = ToolRegistry()
    for tool in BUILTIN_TOOLS:
        registry.register(tool)
    return registry


__all__ = ["BUILTIN_TOOLS", "knowledge", "ocr", "parse", "register_builtins"]
