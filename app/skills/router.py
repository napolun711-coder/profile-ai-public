"""P2A：规则优先的技能路由。

- 每个技能自己实现 `can_handle()`，返回 0～1 的置信度；本模块不调用 LLM；
- 只考虑 `enabled_by_default=True` 的技能；阈值默认 0.5；
- 同分按 `priority` 降序、名字升序，保证确定性；
- 没有候选时用构造时指定的 `fallback`；`force` 可绕过阈值/启用位（调试/测试用）；
- `can_handle()` 抛异常、返回 NaN/inf/非数字，一律按 0 处理。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from app.skills.contracts import Skill, SkillInput
from app.skills.registry import SkillNotFoundError, SkillRegistry


@dataclass(frozen=True)
class RoutingDecision:
    """一次路由结果；`skill=None` 表示没有可用技能。"""

    skill: Optional[Skill]
    name: str = ""
    confidence: float = 0.0
    reason: str = "none"

    def to_dict(self):
        return {
            "skill": self.name,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
        }


class SkillRouter:
    """纯规则路由；不持有工具、不执行技能。"""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        min_confidence: float = 0.5,
        fallback: str = "",
    ) -> None:
        if not isinstance(registry, SkillRegistry):
            raise TypeError("SkillRouter 需要 SkillRegistry")
        if isinstance(min_confidence, bool) or not isinstance(
            min_confidence, (int, float)
        ):
            raise ValueError("min_confidence 必须是数字")
        if not math.isfinite(float(min_confidence)) or not 0.0 <= float(min_confidence) <= 1.0:
            raise ValueError("min_confidence 必须在 0～1 之间")
        if not isinstance(fallback, str):
            raise ValueError("fallback 必须是字符串")

        self.registry = registry
        self.min_confidence = float(min_confidence)
        self.fallback = fallback

    def select(self, request: SkillInput, *, force: str = "") -> RoutingDecision:
        """选择技能；`force` 非空时只认这个技能名。"""
        forced = force.strip() if isinstance(force, str) else ""
        if forced:
            try:
                skill = self.registry.get(forced)
            except SkillNotFoundError:
                return RoutingDecision(None, "", 0.0, "force_missing")
            return RoutingDecision(skill, skill.spec.name, 1.0, "force")

        candidates: List[Tuple[float, Skill]] = []
        for skill in self.registry.all():
            if not skill.spec.enabled_by_default:
                continue
            score = self.score(skill, request)
            if score >= self.min_confidence:
                candidates.append((score, skill))

        if candidates:
            score, skill = min(
                candidates,
                key=lambda pair: (
                    -pair[0],
                    -pair[1].spec.priority,
                    pair[1].spec.name,
                ),
            )
            return RoutingDecision(skill, skill.spec.name, score, "rule")

        if self.fallback:
            try:
                skill = self.registry.get(self.fallback)
            except SkillNotFoundError:
                return RoutingDecision(None, "", 0.0, "fallback_missing")
            return RoutingDecision(skill, skill.spec.name, 0.0, "fallback")

        return RoutingDecision(None, "", 0.0, "none")

    def score(self, skill: Skill, request: SkillInput) -> float:
        """把 `can_handle()` 的返回值收敛到 0～1；坏返回值按 0。"""
        try:
            value = skill.can_handle(request)
        except Exception:  # noqa: BLE001 - 单个技能的规则异常不能影响整次路由
            return 0.0

        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if not isinstance(value, (int, float)):
            return 0.0

        number = float(value)
        if not math.isfinite(number):
            return 0.0
        return max(0.0, min(1.0, number))


__all__ = ["RoutingDecision", "SkillRouter"]
