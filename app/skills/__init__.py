"""P2A：Skill 层（契约 + 注册表 + 规则路由 + Manager）。

这一层始终是**加法**：现有 routers / services / clients 不依赖它，P2A 也没有接
`/chat` 或任何生产调用点。真实技能（knowledge_qa / image_lookup）在 P2B/P2C 再加载。

注意：这里只导出契约与运行时，**不导入任何具体技能**，因此 `import app.skills`
不会发起网络请求、不会读知识库、也不会构造 LightRAG/OCR 客户端。
"""

from app.skills.contracts import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_FALLBACK_MISSING,
    ERROR_FORCE_MISSING,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NO_SKILL,
    ERROR_PERMISSION,
    ERROR_SKILL_EXCEPTION,
    ERROR_SKILL_TIMEOUT,
    SKILL_NAME_RE,
    SKILL_STATUSES,
    Skill,
    SkillError,
    SkillInput,
    SkillOutput,
    SkillSpec,
    validate_skill_output,
    validate_skill_spec,
)
from app.skills.manager import SkillManager
from app.skills.registry import (
    DuplicateSkillError,
    SkillNotFoundError,
    SkillRegistry,
    SkillRegistryError,
)
from app.skills.router import RoutingDecision, SkillRouter
from app.skills.runtime import SkillContext, SkillTrace

__all__ = [
    "DuplicateSkillError",
    "ERROR_BUDGET_EXCEEDED",
    "ERROR_FALLBACK_MISSING",
    "ERROR_FORCE_MISSING",
    "ERROR_INTERNAL",
    "ERROR_INVALID_INPUT",
    "ERROR_INVALID_OUTPUT",
    "ERROR_NO_SKILL",
    "ERROR_PERMISSION",
    "ERROR_SKILL_EXCEPTION",
    "ERROR_SKILL_TIMEOUT",
    "RoutingDecision",
    "SKILL_NAME_RE",
    "SKILL_STATUSES",
    "Skill",
    "SkillContext",
    "SkillError",
    "SkillInput",
    "SkillManager",
    "SkillNotFoundError",
    "SkillOutput",
    "SkillRegistry",
    "SkillRegistryError",
    "SkillRouter",
    "SkillSpec",
    "SkillTrace",
    "validate_skill_output",
    "validate_skill_spec",
]
