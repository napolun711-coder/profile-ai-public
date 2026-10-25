"""P0：显式工具注册表。

刻意保持“笨”：
- 纯内存字典，不做文件扫描、不做模块自动发现、不做网络探活；
- 重名直接报错（不覆盖），避免同一个名字出现两套行为；
- `load_builtin_tools()` 只在调用时导入 adapters，因此 `import app.plugins` 没有副作用。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.plugins.contracts import Tool, ToolSpec, validate_spec


class ToolRegistryError(Exception):
    """注册表错误的基类。"""


class DuplicateToolError(ToolRegistryError):
    """同名工具重复注册。"""


class ToolNotFoundError(ToolRegistryError):
    """按名字找不到工具。"""


class ToolRegistry:
    """一次进程内使用的工具表；测试可以各自创建，互不污染。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """注册一个工具；返回它本身，便于链式使用。"""
        spec = getattr(tool, "spec", None)
        if not isinstance(spec, ToolSpec):
            raise TypeError("注册的工具必须提供 ToolSpec 类型的 spec 属性")
        if not callable(getattr(tool, "invoke", None)):
            raise TypeError("注册的工具必须提供 invoke()")
        if not callable(getattr(tool, "health", None)):
            raise TypeError("注册的工具必须提供 health()")
        validate_spec(spec)
        if spec.name in self._tools:
            raise DuplicateToolError(f"工具已注册：{spec.name}")
        self._tools[spec.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """按名字取工具；找不到时抛 ToolNotFoundError。"""
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise ToolNotFoundError(f"未注册的工具：{name}")
        return tool

    def all(self) -> List[Tool]:
        """按名字排序返回全部工具（输出稳定，便于设置页/测试比对）。"""
        return [self._tools[name] for name in sorted(self._tools)]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def clear(self) -> None:
        """清空注册表（只给测试/热重载用）。"""
        self._tools.clear()

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def load_builtin_tools(registry: Optional[ToolRegistry] = None) -> ToolRegistry:
    """把三个内置适配器注册进给定注册表；不传则新建一个。

    注意用 `is None` 判断：空注册表的 `len()==0`，不能写成 `registry or ...`。
    """
    if registry is None:
        registry = ToolRegistry()

    # 延迟导入：registry 模块本身保持轻量，adapters 只在显式加载时进来。
    from app.plugins.adapters import register_builtins

    return register_builtins(registry)
