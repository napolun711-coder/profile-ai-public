"""工具插件层（P0 契约 + 注册表；P1A 运行时 + 审计）。

这一层始终是**加法**：现有服务不依赖它，P1A 也没有切换任何生产调用点。
规范见 docs/设计_P0_Tool接口与检索旁路.md 与 docs/设计_P1A_Tool运行时.md。

注意：这里只导出契约、注册表与运行时，**不导入 adapters**。
内置适配器由调用方显式调用 `load_builtin_tools()` / `register_builtins()` 时才加载，
因此 `import app.plugins` 不会发起网络请求、不会扫描文件、也不会构造 OCR/LightRAG 客户端。
"""

from app.plugins.audit import AuditLogger
from app.plugins.contracts import (
    ERROR_CIRCUIT_OPEN,
    ERROR_INTERNAL,
    ERROR_INVALID_INPUT,
    ERROR_INVALID_OUTPUT,
    ERROR_NOT_FOUND,
    ERROR_OCR_FAILED,
    ERROR_PARSE_FAILED,
    ERROR_PERMISSION,
    ERROR_TIMEOUT,
    ERROR_UPSTREAM,
    RETRYABLE_UPSTREAM_STATUS,
    RISKS,
    SIDE_EFFECTS,
    Evidence,
    Health,
    RetryPolicy,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    validate_spec,
)
from app.plugins.manager import ToolManager, ToolPolicy, check_path
from app.plugins.registry import (
    DuplicateToolError,
    ToolNotFoundError,
    ToolRegistry,
    ToolRegistryError,
    load_builtin_tools,
)
from app.plugins.schema import check as check_schema

__all__ = [
    "AuditLogger",
    "DuplicateToolError",
    "ERROR_CIRCUIT_OPEN",
    "ERROR_INTERNAL",
    "ERROR_INVALID_INPUT",
    "ERROR_INVALID_OUTPUT",
    "ERROR_NOT_FOUND",
    "ERROR_OCR_FAILED",
    "ERROR_PARSE_FAILED",
    "ERROR_PERMISSION",
    "ERROR_TIMEOUT",
    "ERROR_UPSTREAM",
    "Evidence",
    "Health",
    "RETRYABLE_UPSTREAM_STATUS",
    "RISKS",
    "RetryPolicy",
    "SIDE_EFFECTS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolManager",
    "ToolNotFoundError",
    "ToolPolicy",
    "ToolRegistry",
    "ToolRegistryError",
    "ToolResult",
    "ToolSpec",
    "check_path",
    "check_schema",
    "load_builtin_tools",
    "validate_spec",
]
