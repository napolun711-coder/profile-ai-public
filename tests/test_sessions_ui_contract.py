"""M1：前端会话同步的静态契约（防止只做后端、页面仍只存 localStorage）。"""

from __future__ import annotations

import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
INDEX = BASE_DIR / "static" / "index.html"
COMPANION = BASE_DIR / "static" / "companion.js"


class SessionsUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = INDEX.read_text(encoding="utf-8")
        cls.companion = COMPANION.read_text(encoding="utf-8")

    def test_request_carries_session_and_client_message_id(self) -> None:
        self.assertIn("payload.session_id = sessionId;", self.html)
        self.assertIn("payload.client_message_id = clientMessageId;", self.html)
        self.assertIn("const clientMessageId = newClientMessageId();", self.html)
        self.assertIn("persistUserMessage(text, clientMessageId);", self.html)

    def test_chat_composer_keeps_base_display_styles(self) -> None:
        # 输入区不能用浏览器默认 textarea：否则附件、输入框和发送键会散开。
        for token in (
            ".composer-inner{",
            "display:flex;gap:8px;align-items:flex-end;min-width:0;",
            ".composer-inner:focus-within{",
            "#input{",
            "flex:1 1 0;width:100%;min-width:0;box-sizing:border-box;",
            "border:0;outline:0;resize:none;overflow-y:auto;",
            ".composer-inner #send{",
            ".composer-inner:not(.has-text):not(.busy) #send{opacity:.5}",
        ):
            self.assertIn(token, self.html, token)

    def test_manual_skill_route_is_removed_from_chat(self) -> None:
        for token in (
            'id="skillRouteBtn"',
            "skillRoutingEnabled",
            "payload.skills_enabled",
            'event.type === "skill_route"',
            "skill-route-row",
        ):
            self.assertNotIn(token, self.html, token)

    def test_server_session_header_is_adopted(self) -> None:
        self.assertIn('response.headers.get("X-Session-Id")', self.html)
        self.assertIn('fallback.headers.get("X-Session-Id")', self.html)
        self.assertIn("adoptServerSessionId(serverSessionId)", self.html)

    def test_session_api_is_used(self) -> None:
        for token in (
            "apiListSessions",
            "apiCreateSession",
            "apiGetMessages",
            "apiDeleteSession",
            "apiClearMessages",
            "apiImportSessions",
            "loadSessionMessages",
            "ensureCurrentSession",
            "serverSessionToLocal",
            "serverMessageToLocal",
        ):
            self.assertIn(token, self.html, token)

    def test_local_storage_import_is_wired(self) -> None:
        self.assertIn("chat_sessions_migrated_v2", self.html)
        self.assertIn('localStorage.getItem(SESSION_MIGRATED)', self.html)
        self.assertIn("messages: item.messages || []", self.html)
        self.assertIn("apiImportSessions(payload)", self.html)

    def test_fallback_when_backend_disabled(self) -> None:
        self.assertIn("SERVER_SESSIONS.enabled", self.html)
        self.assertIn("后端未启用 / 不可达", self.html)

    def test_concurrent_session_creation_is_guarded(self) -> None:
        # 初始化建会话与用户点击发送可能同时发生；必须有共享 promise + 发送去重。
        self.assertIn("let sessionCreatePromise = null;", self.html)
        self.assertIn("sessionCreatePromise = _startNewSession()", self.html)
        self.assertIn("let sendInFlight = false;", self.html)
        self.assertIn("if (!text || busy || sendInFlight)", self.html)

    def test_no_context_manual_continue_is_wired(self) -> None:
        self.assertIn("payload.force_bypass = true;", self.html)
        self.assertIn('event.type === "no_context"', self.html)
        self.assertIn("继续对话（不查资料）", self.html)
        self.assertIn("forceBypass: true", self.html)
        self.assertIn("本轮未使用知识库", self.html)

    def test_context_failure_is_visible_not_silent(self) -> None:
        # 后端状态头 + 流式 context 事件 + 回答内提示，三者都要接通。
        self.assertIn('response.headers.get("X-Context-Status")', self.html)
        self.assertIn('fallback.headers.get("X-Context-Status")', self.html)
        self.assertIn('event.type === "context"', self.html)
        self.assertIn("appendContextWarning(view)", self.html)
        self.assertIn(".context-warning{", self.html)
        self.assertIn("本轮未带入会话上下文", self.html)

    def test_voice_reading_progress_and_refresh_guard_are_wired(self) -> None:
        self.assertIn("view.__restored = true", self.html)
        self.assertIn("::highlight(chat-reading)", self.html)
        self.assertIn(".refs,.img-refs{", self.html)
        voice = (BASE_DIR / "static" / "voice.js").read_text(
            encoding="utf-8"
        )
        for token in (
            "autoSpeak: false",
            "browserChunkChars: 180",
            "function beginReadingProgress(view)",
            "function updateReadingProgress(ratio, options)",
            "function setReadingUnit(index, options)",
            "window.CSS.highlights.set(READING_HIGHLIGHT",
            "paintReadingFallback(range)",
            "reading-overlay",
            "reading-pulse-layer",
            "utter.onboundary",
            'syncMode: "audio-time"',
            "readingFollowPaused",
            "pauseReadingFollow()",
            "motion: \"reading\"",
            "scheduleSpeak(view, false);",
            "CONFIG.autoSpeak = true;",
        ):
            self.assertIn(token, voice, token)

    def test_work_companion_is_wired_to_real_task_events(self) -> None:
        self.assertIn('id="workCompanion"', self.html)
        self.assertIn('id="workLabel"', self.html)
        self.assertIn('src="/static/companion.js"', self.html)
        self.assertIn("WorkCompanion.begin", self.html)
        self.assertIn("WorkCompanion.ingest(card.__workId", self.html)
        self.assertIn("noteRecoveredSessionWork(session)", self.html)
        self.assertIn("const active = data.active || [];", self.html)
        self.assertIn('event.type === "references"', self.html)
        self.assertIn('@media (prefers-reduced-motion:reduce)', self.html)

    def test_companion_state_machine_keeps_task_identity(self) -> None:
        for token in (
            'const tasks = new Map();',
            "function begin(id, info)",
            "function update(id, info)",
            "function finish(id, info)",
            "function ingest(id, data, options)",
            "task.id = String(id);",
            "const active = Array.from(tasks.values()).filter",
            "正在处理资料队列…",
        ):
            self.assertIn(token, self.companion, token)
        # 机器人不生成虚假百分比：百分比只能来自后端真实 processed/total。
        self.assertNotIn("style.width", self.companion)

    def test_companion_uses_interactive_wireframe_cube(self) -> None:
        for token in (
            'id="workCubeStage"',
            'id="workCubeModel"',
            'class="wc-cube"',
            'class="wc-face wc-face--front"',
            'class="wc-face wc-face--back"',
            'class="wc-face wc-face--right"',
            'class="wc-face wc-face--left"',
            'class="wc-face wc-face--top"',
            'class="wc-face wc-face--bottom"',
            'class="wc-cube-core"',
            "transform-style:preserve-3d",
            "perspective:300px",
            "background:transparent;border:0;box-shadow:none",
        ):
            self.assertIn(token, self.html, token)
        for token in (
            "cubeAngles",
            'addEventListener("pointerdown"',
            'addEventListener("pointermove"',
            "requestAnimationFrame(cubeFrame)",
            "activityMode",
            "DOCK_POSITION_KEY",
        ):
            self.assertIn(token, self.companion, token)


if __name__ == "__main__":
    unittest.main()
