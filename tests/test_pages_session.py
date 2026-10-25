"""Phase 16.1：页面「会话心跳」接线检查（纯静态、秒级、不联网）。

为什么值得一个测试：Phase 10 的「关窗口才停服」靠的是**每个页面都引 session.js** ——
从一个页面跳到另一个页面时，旧页面 pagehide 已经用 sendBeacon 预约了「25 秒后停服」，
只有新页面继续 keepalive 才会把这次预约取消。`static\\files.html` 漏引这一行时，
用户在「文件管理」页翻资料，服务会连同 LightRAG / Qdrant 一起被停掉
（真机事故：logs\\stop.log 两次「原因：tab-close」，LightRAG 侧没有任何报错）。
这类问题不报错、只在 20 多秒后表现为「服务莫名没了」，所以用静态检查兜底。

覆盖：
    1. `static\\` 下每个页面都引了 `/static/session.js`（白名单为空，将来加页面也会被兜住）；
    2. 文件管理页有「正在停止 / 服务已停止」遮罩与「重新检测」按钮，并监听 `pai-session`；
    3. `session.js` 仍然指向约定的后端接口 —— 接口名被改掉时先在这儿报错，
       而不是让用户遇到「关窗口后服务没停 / 莫名停了」。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: 允许不引 session.js 的页面（目前是**空集合**：加白名单必须在评审里写清理由）
NO_SESSION_PAGES: frozenset = frozenset()

#: 引法兼容 `defer` / `async` / 单双引号：只要 src 指向 /static/session.js 就算
SESSION_SCRIPT = re.compile(r"""<script[^>]*src=["']/static/session\.js["'][^>]*>""", re.I)

#: session.js 必须打到的后端接口（改动接口名时这里先红）
SESSION_ENDPOINTS = (
    "/api/app/session",
    "/api/app/keepalive",
    "/api/app/leave",
    "/api/app/stop",
    "/api/app/control",
)


class PageSessionTagTests(unittest.TestCase):
    """每个页面都要挂心跳（转发链路上少一环，用户就看到「服务莫名停了」）。"""

    def test_every_page_links_the_session_script(self) -> None:
        pages = sorted(STATIC_DIR.glob("*.html"))
        self.assertTrue(pages, "static\\ 下没有页面？测试路径可能不对")
        missing = [
            page.name
            for page in pages
            if page.name not in NO_SESSION_PAGES
            and not SESSION_SCRIPT.search(page.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            missing,
            [],
            f"这些页面没引 /static/session.js（心跳 / 关窗口停服会失效）：{missing}",
        )


class FilesPageOverlayTests(unittest.TestCase):
    """文件管理页的「服务已停止」提示：看得见、点得动，且遮罩在脚本之前。"""

    def setUp(self) -> None:
        self.html = (STATIC_DIR / "files.html").read_text(encoding="utf-8")

    def test_overlay_and_stop_button_exist(self) -> None:
        for anchor in (
            'id="stopping"',
            'id="stoppingText"',
            'id="offline"',
            'id="offlineText"',
            'id="offlineHint"',
            'id="offlineRetry"',
            'id="stopBtn"',
        ):
            self.assertIn(anchor, self.html, f"files.html 缺少 {anchor}")

    def test_overlay_is_declared_before_the_script_uses_it(self) -> None:
        # 脚本里用 $("offline") 取节点：DOM 里晚于脚本就等于取到 null
        self.assertLess(self.html.index('id="offline"'), self.html.index("function renderSession"))

    def test_page_listens_to_session_events(self) -> None:
        self.assertIn('addEventListener("pai-session"', self.html)
        self.assertIn("window.PAI_SESSION", self.html)

    def test_stop_button_is_local_only(self) -> None:
        # 按钮初始 hidden，只有后端说 can_stop 才显示（手机端后端也会 403）
        self.assertIn('id="stopBtn" class="danger" hidden', self.html)
        self.assertIn('$("stopBtn").hidden = !info.canStop', self.html)


class SessionScriptTests(unittest.TestCase):
    """session.js 与后端约定的接口名，改动前先在这里红一次。"""

    def setUp(self) -> None:
        self.js = (STATIC_DIR / "session.js").read_text(encoding="utf-8")

    def test_endpoints_are_unchanged(self) -> None:
        for endpoint in SESSION_ENDPOINTS:
            self.assertIn(endpoint, self.js, f"session.js 不再调用 {endpoint}")

    def test_heartbeat_is_faster_than_the_local_countdown(self) -> None:
        match = re.search(r"HEARTBEAT_MS\s*=\s*(\d+)", self.js)
        self.assertIsNotNone(match, "session.js 里找不到 HEARTBEAT_MS")
        heartbeat = int(match.group(1))
        self.assertLess(heartbeat, 25000, "心跳间隔必须明显小于本机停服倒计时（25 秒）")


if __name__ == "__main__":
    unittest.main()
