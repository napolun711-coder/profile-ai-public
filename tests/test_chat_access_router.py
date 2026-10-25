"""阶段 0：确认 /api/sessions 与 /api/memory 的路由级依赖真的接上了。

环境没有 httpx/httpx2（不为此新增依赖），所以这里做**结构断言**：
路由的 dependencies 必须包含 require_chat_access；鉴权行为本身由
tests/test_chat_access.py 用最小 Request 覆盖。
"""

from __future__ import annotations

import unittest

from app.core.chat_access import require_chat_access
from app.routers import memory as memory_router
from app.routers import sessions as sessions_router


class RouterGuardWiringTests(unittest.TestCase):
    def test_sessions_router_has_access_guard(self) -> None:
        deps = sessions_router.router.dependencies
        self.assertTrue(
            any(getattr(dep, "dependency", None) is require_chat_access for dep in deps),
            "sessions 路由缺少 require_chat_access 依赖",
        )

    def test_memory_router_has_access_guard(self) -> None:
        deps = memory_router.router.dependencies
        self.assertTrue(
            any(getattr(dep, "dependency", None) is require_chat_access for dep in deps),
            "memory 路由缺少 require_chat_access 依赖",
        )

if __name__ == "__main__":
    unittest.main()
