"""M1：对话持久化 / 记忆系统（本地 SQLite）。

边界：
- 只有 `MEMORY_ENABLED=on` 才会创建/写入数据库；
- `MEMORY_ENABLED=off` 时本包的初始化函数直接返回，聊天路径完全不触碰磁盘；
- M1 只负责原始对话（sessions/messages）与 FTS 索引；长期记忆、提取、任务队列在后续阶段。
"""

from app.memory.runtime import get_store, initialize, enabled, reset_for_tests, status

__all__ = [
    "enabled",
    "get_store",
    "initialize",
    "reset_for_tests",
    "status",
]
