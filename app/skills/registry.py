"""P2A：显式技能注册表。

刻意保持“笨”：
- 纯内存字典，不扫描目录、不自动发现模块、不导入真实技能；
- 重名直接报错（不覆盖）；
- 注册时校验 `SkillSpec` 与 `can_handle()` / `execute()`，坏技能不会进入表里；
- `all()` 按名字排序，路由与测试输出稳定。
"""

from __future__ import annotations

from typing import Dict, List

from app.skills.contracts import Skill, SkillSpec, validate_skill_spec


class SkillRegistryError(Exception):
    """注册表错误的基类。"""


class DuplicateSkillError(SkillRegistryError):
    """同名技能重复注册。"""


class SkillNotFoundError(SkillRegistryError):
    """按名字找不到技能。"""


class SkillRegistry:
    """一次进程内使用的技能表；测试可以各自创建，互不污染。"""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> Skill:
        """注册一个技能并返回它本身；不合法时抛 TypeError/ValueError。"""
        spec = getattr(skill, "spec", None)
        if not isinstance(spec, SkillSpec):
            raise TypeError("注册的技能必须提供 SkillSpec 类型的 spec 属性")
        if not callable(getattr(skill, "can_handle", None)):
            raise TypeError("注册的技能必须提供 can_handle()")
        if not callable(getattr(skill, "execute", None)):
            raise TypeError("注册的技能必须提供 execute()")
        validate_skill_spec(spec)
        if spec.name in self._skills:
            raise DuplicateSkillError(f"技能已注册：{spec.name}")
        self._skills[spec.name] = skill
        return skill

    def get(self, name: str) -> Skill:
        """按名字取技能；找不到时抛 SkillNotFoundError。"""
        skill = self._skills.get(name) if isinstance(name, str) else None
        if skill is None:
            raise SkillNotFoundError(f"未注册的技能：{name}")
        return skill

    def all(self) -> List[Skill]:
        """按名字排序返回全部技能。"""
        return [self._skills[name] for name in sorted(self._skills)]

    def names(self) -> List[str]:
        return sorted(self._skills)

    def clear(self) -> None:
        """清空注册表（只给测试/热重载用）。"""
        self._skills.clear()

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def __len__(self) -> int:
        return len(self._skills)


__all__ = [
    "DuplicateSkillError",
    "SkillNotFoundError",
    "SkillRegistry",
    "SkillRegistryError",
]
