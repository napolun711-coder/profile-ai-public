"""P1A：工具清单只读接口。

  GET /api/tools            列出内置工具与当前策略状态
  GET /api/tools?schema=1   额外附带 input_schema / output_schema

设计口径：
- **只读**：不注册工具、不探活、不联网、不写任何文件；
- 注册表懒加载 + 进程内缓存（不在 `main.py` 启动时注册，与 P0 的“导入无副作用”一致）；
- `enabled` / `policy_reason` 由默认 `ToolPolicy` 计算：外部工具默认关闭，
  因此 `knowledge.search` / `ocr.recognize` 会显示为不可用（原因 `external_disabled`）。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from app.plugins.manager import ToolPolicy
from app.plugins.registry import ToolRegistry, load_builtin_tools

router = APIRouter(prefix="/api/tools", tags=["tools"])

_cache_lock = threading.Lock()
_cached_registry: Optional[ToolRegistry] = None


def default_registry() -> ToolRegistry:
    """懒加载内置工具表；首次访问时加载，之后复用（进程内单例）。"""
    global _cached_registry
    if _cached_registry is None:
        with _cache_lock:
            if _cached_registry is None:
                _cached_registry = load_builtin_tools()
    return _cached_registry


def reset_registry_cache() -> None:
    """清空缓存（只给测试用；生产请求不会调用）。"""
    global _cached_registry
    with _cache_lock:
        _cached_registry = None


def tool_catalog(
    *,
    with_schema: bool = False,
    registry: Optional[ToolRegistry] = None,
    policy: Optional[ToolPolicy] = None,
) -> Dict[str, Any]:
    """组装工具清单（纯读；测试可直接注入 registry/policy）。"""
    source = registry if registry is not None else default_registry()
    active = policy if policy is not None else ToolPolicy()

    tools: List[Dict[str, Any]] = []
    for tool in source.all():
        spec = tool.spec
        enabled, reason = active.evaluate(spec, caller="api")
        row: Dict[str, Any] = {
            "name": spec.name,
            "version": spec.version,
            "description": spec.description,
            "capabilities": list(spec.capabilities),
            "side_effect": spec.side_effect,
            "external": spec.external,
            "risk": spec.risk,
            "timeout_s": spec.timeout_s,
            "retry": {
                "max_attempts": spec.retry.max_attempts,
                "backoff_s": spec.retry.backoff_s,
                "retry_on": list(spec.retry.retry_on),
            },
            "allowed_for_skills": spec.allowed_for_skills,
            "enabled": enabled,
            "policy_reason": reason,
        }
        if with_schema:
            row["input_schema"] = spec.input_schema
            row["output_schema"] = spec.output_schema
        tools.append(row)

    return {
        "ok": True,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": {
            "allowed_external": active.allowed_external,
            "allowed_side_effects": list(active.allowed_side_effects),
        },
        "tools": tools,
    }


@router.get("")
def list_tools(schema: bool = False) -> Dict[str, Any]:
    """只读清单；`schema=1` 时才附带参数/结果 schema。"""
    return tool_catalog(with_schema=bool(schema))


__all__ = [
    "default_registry",
    "list_tools",
    "reset_registry_cache",
    "router",
    "tool_catalog",
]
