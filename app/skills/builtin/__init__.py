"""P2B：显式内置技能。

`app/skills/__init__.py` **不导入**本模块，因此 `import app.skills` 仍然只加载契约与
运行时；影子脚本/测试必须显式 `from app.skills import builtin` 才会构造真实技能。

不做目录扫描、不做自动发现：当前只有 `knowledge_qa`。
"""

from __future__ import annotations

from typing import Optional

from app.skills.builtin import knowledge_qa
from app.skills.registry import SkillRegistry

BUILTIN_SKILLS = (knowledge_qa.SKILL,)


def register_builtin_skills(registry: Optional[SkillRegistry] = None) -> SkillRegistry:
    """把内置技能注册进给定注册表；不传则新建一个。"""
    if registry is None:
        registry = SkillRegistry()
    for skill in BUILTIN_SKILLS:
        registry.register(skill)
    return registry


__all__ = ["BUILTIN_SKILLS", "knowledge_qa", "register_builtin_skills"]
