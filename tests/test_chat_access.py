"""局域网设备一键确认授权的鉴权边界。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request

from app.core import access_devices, chat_access
from app.core.config import get_settings


def make_request(
    host: str,
    token: str = "",
    cookie: str = "",
    forwarded: str = "",
) -> Request:
    headers = []
    if token:
        headers.append((b"x-device-token", token.encode("utf-8")))
    if cookie:
        headers.append((b"cookie", ("pai_device_token=" + cookie).encode("utf-8")))
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode("utf-8")))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "headers": headers,
        "client": (host, 51234),
        "query_string": b"",
    })


class ChatAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.device_file = Path(self._tmp.name) / "access_devices.json"
        self._patches = [
            mock.patch.object(access_devices, "TOKEN_FILE", self.device_file),
            mock.patch.object(
                chat_access,
                "get_settings",
                return_value=replace(get_settings(), app_host="0.0.0.0"),
            ),
        ]
        for patch in self._patches:
            patch.start()
        access_devices.reset_for_tests()

    def tearDown(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._tmp.cleanup()

    def _paired_token(self, name: str = "测试平板") -> str:
        code = access_devices.create_pairing(created_by_ip="127.0.0.1")["code"]
        token, _ = access_devices.pair_device(code, name, client_ip="192.168.3.50")
        return token

    def test_local_client_bypasses_device_authorization(self) -> None:
        for host in ("127.0.0.1", "::1", "localhost"):
            chat_access.require_chat_access(make_request(host))

    def test_lan_without_device_token_requires_one_click_approval(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            chat_access.require_chat_access(make_request("192.168.3.50"))
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(caught.exception.detail["error"], "device_auth_required")
        self.assertIn("电脑端", caught.exception.detail["message"])
        self.assertIn("允许", caught.exception.detail["message"])
        self.assertEqual(caught.exception.detail["pair_url"], "/pair")

    def test_loopback_proxy_uses_forwarded_lan_client_address(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            chat_access.require_chat_access(
                make_request("127.0.0.1", forwarded="192.168.3.50")
            )
        self.assertEqual(caught.exception.status_code, 401)

    def test_direct_lan_request_cannot_spoof_forwarded_local_address(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            chat_access.require_chat_access(
                make_request("192.168.3.50", forwarded="127.0.0.1")
            )
        self.assertEqual(caught.exception.status_code, 401)

    def test_lan_accepts_header_or_cookie_device_token(self) -> None:
        token = self._paired_token()
        chat_access.require_chat_access(make_request("192.168.3.50", token=token))
        chat_access.require_chat_access(make_request("192.168.3.50", cookie=token))

    def test_invalid_or_deleted_device_is_rejected(self) -> None:
        with self.assertRaises(HTTPException):
            chat_access.require_chat_access(make_request("192.168.3.50", token="wrong"))

        token = self._paired_token("待删除设备")
        device = access_devices.authenticate_device(token)
        self.assertIsNotNone(device)
        self.assertTrue(access_devices.delete_device(device["id"]))
        with self.assertRaises(HTTPException):
            chat_access.require_chat_access(make_request("192.168.3.50", token=token))

    def test_old_global_password_is_not_accepted(self) -> None:
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/sessions",
            "headers": [(b"x-chat-token", b"old-password")],
            "client": ("192.168.3.50", 51234),
            "query_string": b"",
        })
        with self.assertRaises(HTTPException) as caught:
            chat_access.require_chat_access(request)
        self.assertEqual(caught.exception.status_code, 401)

    def test_local_only_write_rejects_lan(self) -> None:
        chat_access.require_local_access(make_request("127.0.0.1"))
        chat_access.require_local_access(make_request("::1"))
        with self.assertRaises(HTTPException) as caught:
            chat_access.require_local_access(make_request("192.168.3.50"))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(caught.exception.detail["error"], "local_only")

    def test_local_only_mode_needs_no_device_auth(self) -> None:
        with mock.patch.object(
            chat_access,
            "get_settings",
            return_value=replace(get_settings(), app_host="127.0.0.1"),
        ):
            chat_access.require_chat_access(make_request("192.168.3.50"))
            state = chat_access.access_state(make_request("192.168.3.50"))
            self.assertFalse(state["enabled"])
            self.assertTrue(state["authorized"])

    def test_access_state_reports_paired_device(self) -> None:
        token = self._paired_token("客厅平板")
        state = chat_access.access_state(make_request("192.168.3.50", cookie=token))
        self.assertTrue(state["authorized"])
        self.assertFalse(state["local"])
        self.assertEqual(state["device"]["name"], "客厅平板")


if __name__ == "__main__":
    unittest.main()
