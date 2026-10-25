"""Batch 2：/api/ai-bridge 路由函数（不启动 HTTP 服务，直接调用）。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response
from starlette.requests import Request

from app.routers import ai_bridge


def make_request(body: bytes = b"", headers: dict | None = None) -> Request:
    raw = [
        (key.lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ai-bridge/ingest",
        "headers": raw,
        "query_string": b"",
    }
    return Request(scope, receive)


def settings(
    enabled: bool = True,
    ttl: int = 300,
    max_chars: int = 65536,
    auto_send: bool = False,
):
    return SimpleNamespace(
        ai_bridge_enabled=enabled,
        ai_bridge_ttl_seconds=ttl,
        ai_bridge_max_chars=max_chars,
        ai_bridge_auto_send=auto_send,
    )


class AiBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ai_bridge.reset_for_tests()

    def auth(self) -> dict:
        return {
            "X-AI-Bridge-Token": ai_bridge._TOKEN,
            "Content-Type": "text/plain; charset=utf-8",
        }

    def test_status_disabled(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings(enabled=False)):
            data = ai_bridge.status()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["token"], "")

    def test_status_enabled_exposes_process_token(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            data = ai_bridge.status()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["token"], ai_bridge._TOKEN)

    def test_status_auto_send_flag(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings(auto_send=True)):
            data = ai_bridge.status()
        self.assertTrue(data["auto_send"])
        with patch.object(ai_bridge, "get_settings", return_value=settings(auto_send=False)):
            data = ai_bridge.status()
        self.assertFalse(data["auto_send"])

    async def test_ingest_disabled_404(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings(enabled=False)):
            with self.assertRaises(HTTPException) as caught:
                await ai_bridge.ingest(make_request(b"x", self.auth()))
        self.assertEqual(caught.exception.status_code, 404)

    async def test_ingest_requires_token(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            with self.assertRaises(HTTPException) as caught:
                await ai_bridge.ingest(
                    make_request(b"x", {"Content-Type": "text/plain; charset=utf-8"})
                )
        self.assertEqual(caught.exception.status_code, 403)

    async def test_roundtrip_and_consume(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            result = await ai_bridge.ingest(make_request("系统提示词".encode(), self.auth()))
            self.assertTrue(result["ok"])
            item = await ai_bridge.pending(
                make_request(headers={"X-AI-Bridge-Token": ai_bridge._TOKEN})
            )
            self.assertEqual(item["text"], "系统提示词")
            second = await ai_bridge.pending(
                make_request(headers={"X-AI-Bridge-Token": ai_bridge._TOKEN})
            )
            self.assertIsInstance(second, Response)
            self.assertEqual(second.status_code, 204)

    async def test_duplicate_within_window(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            first = await ai_bridge.ingest(make_request("同一段".encode(), self.auth()))
            second = await ai_bridge.ingest(make_request("同一段".encode(), self.auth()))
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            item = await ai_bridge.pending(
                make_request(headers={"X-AI-Bridge-Token": ai_bridge._TOKEN})
            )
            self.assertEqual(item["text"], "同一段")

    async def test_empty_rejected(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            with self.assertRaises(HTTPException) as caught:
                await ai_bridge.ingest(make_request(b"   ", self.auth()))
        self.assertEqual(caught.exception.status_code, 400)

    async def test_too_long_rejected(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings(max_chars=256)):
            with self.assertRaises(HTTPException) as caught:
                await ai_bridge.ingest(make_request(("x" * 257).encode(), self.auth()))
        self.assertEqual(caught.exception.status_code, 413)

    async def test_wrong_content_type_rejected(self) -> None:
        headers = {
            "X-AI-Bridge-Token": ai_bridge._TOKEN,
            "Content-Type": "application/json",
        }
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            with self.assertRaises(HTTPException) as caught:
                await ai_bridge.ingest(make_request(b"{}", headers))
        self.assertEqual(caught.exception.status_code, 415)

    async def test_ttl_expires(self) -> None:
        with patch.object(ai_bridge, "get_settings", return_value=settings(ttl=5)):
            with patch.object(ai_bridge.time, "monotonic", return_value=100.0):
                await ai_bridge.ingest(make_request("过期".encode(), self.auth()))
            with patch.object(ai_bridge.time, "monotonic", return_value=106.0):
                item = await ai_bridge.pending(
                    make_request(headers={"X-AI-Bridge-Token": ai_bridge._TOKEN})
                )
        self.assertIsInstance(item, Response)
        self.assertEqual(item.status_code, 204)

    async def test_logs_do_not_contain_text(self) -> None:
        secret = "SECRET_PROMPT_TEXT_123"
        with patch.object(ai_bridge, "get_settings", return_value=settings()):
            with self.assertLogs("app.routers.ai_bridge", level="INFO") as captured:
                await ai_bridge.ingest(make_request(secret.encode(), self.auth()))
        joined = "\n".join(captured.output)
        self.assertNotIn(secret, joined)
        self.assertIn("chars=", joined)

    def test_router_does_not_import_memory(self) -> None:
        tree = ast.parse(Path(ai_bridge.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        self.assertFalse(
            any(name == "app.memory" or name.startswith("app.memory.") for name in imported),
            imported,
        )
