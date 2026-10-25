"""One-click launcher readiness and failure diagnostics."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import start  # noqa: E402 - launcher is a script, not a package
from app.routers import system  # noqa: E402


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _ExitedProcess:
    pid = 4242

    def poll(self):
        return 1


class LauncherStartupTests(unittest.TestCase):
    def test_live_probe_only_accepts_this_app(self) -> None:
        self.assertTrue(start.app_live_ok(_Response({"app": "ok", "status": "ready"})))
        self.assertFalse(start.app_live_ok(_Response({"status": "ready"})))
        self.assertFalse(start.app_live_ok(_Response([])))

    def test_dead_app_process_fails_without_waiting(self) -> None:
        with mock.patch.object(start.requests, "get") as request:
            ready = start.wait_http(
                "http://127.0.0.1:1/health/live",
                180,
                "本应用",
                process=_ExitedProcess(),
                accept=start.app_live_ok,
            )
        self.assertFalse(ready)
        request.assert_not_called()

    def test_failure_diagnostic_contains_exit_and_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logs = Path(temp)
            start_log = logs / "start.log"
            start_log.write_text("launcher line\n", encoding="utf-8")
            diagnostic = logs / "startup-diagnostic.txt"
            console = logs / "app_console.txt"
            console.write_text("uvicorn traceback line\n", encoding="utf-8")

            with mock.patch.object(start, "LOGS", logs), \
                 mock.patch.object(start, "LOG_FILE", start_log), \
                 mock.patch.object(start, "DIAGNOSTIC_FILE", diagnostic), \
                 mock.patch.object(start, "PROFILE_AI_HOME", logs), \
                 mock.patch.object(
                     start.subprocess,
                     "run",
                     return_value=SimpleNamespace(stdout=b"", returncode=0),
                 ):
                start.write_startup_diagnostic(8100, _ExitedProcess())

            body = diagnostic.read_text(encoding="utf-8")
            self.assertIn("退出码 1", body)
            self.assertIn("uvicorn traceback line", body)
            self.assertIn("launcher line", body)

    def test_liveness_route_is_dependency_free(self) -> None:
        self.assertEqual(system.health_live(), {"app": "ok", "status": "ready"})


if __name__ == "__main__":
    unittest.main()
