"""局域网一键确认、六位码兼容配对与设备持久化。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import access_devices


class AccessDeviceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "access_devices.json"
        self.patcher = mock.patch.object(access_devices, "TOKEN_FILE", self.path)
        self.patcher.start()
        access_devices.reset_for_tests()

    def tearDown(self) -> None:
        self.patcher.stop()
        self._tmp.cleanup()

    def test_pairing_code_is_one_time_and_secrets_are_hashed(self) -> None:
        code = access_devices.create_pairing(created_by_ip="127.0.0.1")["code"]
        self.assertRegex(code, r"^\d{6}$")
        token, device = access_devices.pair_device(
            code, "客厅平板", client_ip="192.168.3.50", user_agent="test"
        )

        self.assertTrue(token)
        self.assertEqual(device["name"], "客厅平板")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(code, raw)
        self.assertNotIn(token, raw)
        with self.assertRaises(ValueError):
            access_devices.pair_device(code, "重复使用", client_ip="192.168.3.50")

    def test_device_name_and_last_seen_are_persisted(self) -> None:
        code = access_devices.create_pairing()["code"]
        token, created = access_devices.pair_device(code, "  办公  平板  ")
        self.assertEqual(created["name"], "办公 平板")
        devices = access_devices.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["name"], "办公 平板")
        self.assertTrue(devices[0]["created_at"])
        self.assertTrue(devices[0]["last_seen_at"])
        self.assertEqual(access_devices.authenticate_device(token)["name"], "办公 平板")

    def test_deleted_device_token_is_invalid(self) -> None:
        code = access_devices.create_pairing()["code"]
        token, device = access_devices.pair_device(code, "待撤销设备")
        self.assertTrue(access_devices.delete_device(device["id"]))
        self.assertIsNone(access_devices.authenticate_device(token))
        self.assertEqual(access_devices.list_devices(), [])

    def test_new_code_replaces_previous_code(self) -> None:
        old_code = access_devices.create_pairing()["code"]
        new_code = access_devices.create_pairing()["code"]
        with self.assertRaises(ValueError):
            access_devices.pair_device(old_code, "旧码设备")
        _, device = access_devices.pair_device(new_code, "新码设备")
        self.assertEqual(device["name"], "新码设备")

    def test_pairing_code_expires_after_five_minutes(self) -> None:
        with mock.patch.object(access_devices.time, "time", return_value=1000.0):
            created = access_devices.create_pairing()
        self.assertEqual(created["expires_in"], 300)
        with mock.patch.object(access_devices.time, "time", return_value=1301.0):
            with self.assertRaises(ValueError):
                access_devices.pair_device(created["code"], "过期设备")

    def test_empty_name_gets_safe_default(self) -> None:
        code = access_devices.create_pairing()["code"]
        _, device = access_devices.pair_device(code, "   ")
        self.assertEqual(device["name"], "未命名设备")

    def test_one_click_request_is_hashed_approved_and_claimed_once(self) -> None:
        created = access_devices.create_pairing_request(
            "",
            client_ip="192.168.3.50",
            user_agent="Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)",
        )
        self.assertTrue(created["name"].startswith("iPad · "))
        self.assertEqual(created["expires_in"], 300)
        pending = access_devices.list_pairing_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], created["request_id"])

        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(created["request_secret"], raw)
        self.assertNotIn("request_secret", raw)

        token, device, status = access_devices.claim_pairing_request(
            created["request_id"],
            created["request_secret"],
            client_ip="192.168.3.50",
        )
        self.assertEqual(status, "pending")
        self.assertIsNone(token)
        self.assertIsNone(device)

        approved = access_devices.approve_pairing_request(created["request_id"])
        self.assertEqual(approved["name"], created["name"])
        self.assertEqual(access_devices.list_pairing_requests(), [])

        token, device, status = access_devices.claim_pairing_request(
            created["request_id"],
            created["request_secret"],
            client_ip="192.168.3.50",
        )
        self.assertEqual(status, "approved")
        self.assertTrue(token)
        self.assertEqual(device["name"], created["name"])
        self.assertIsNotNone(access_devices.authenticate_device(token))
        with self.assertRaises(ValueError):
            access_devices.claim_pairing_request(
                created["request_id"],
                created["request_secret"],
                client_ip="192.168.3.50",
            )

    def test_one_click_request_rejects_wrong_secret_and_changed_source(self) -> None:
        created = access_devices.create_pairing_request(
            "书房平板", client_ip="192.168.3.50"
        )
        access_devices.approve_pairing_request(created["request_id"])
        with self.assertRaises(ValueError):
            access_devices.claim_pairing_request(
                created["request_id"], "x" * 40, client_ip="192.168.3.50"
            )
        with self.assertRaises(ValueError):
            access_devices.claim_pairing_request(
                created["request_id"],
                created["request_secret"],
                client_ip="192.168.3.51",
            )

    def test_one_click_request_expires_after_five_minutes(self) -> None:
        with mock.patch.object(access_devices.time, "time", return_value=2000.0):
            created = access_devices.create_pairing_request("平板")
        with mock.patch.object(access_devices.time, "time", return_value=2301.0):
            self.assertEqual(access_devices.list_pairing_requests(), [])
            with self.assertRaises(ValueError):
                access_devices.approve_pairing_request(created["request_id"])

    def test_v1_state_without_requests_remains_readable(self) -> None:
        self.path.write_text(
            '{"version":1,"devices":{},"pairings":{}}\n',
            encoding="utf-8",
        )
        self.assertEqual(access_devices.list_devices(), [])
        self.assertEqual(access_devices.list_pairing_requests(), [])

    def test_six_digit_attempts_are_rate_limited(self) -> None:
        access_devices.create_pairing()
        for _ in range(access_devices.PAIRING_MAX_ATTEMPTS):
            with self.assertRaises(ValueError):
                access_devices.pair_device("000000", "错误尝试", client_ip="192.168.3.60")
        with self.assertRaises(access_devices.PairingRateLimitError):
            access_devices.pair_device("000000", "错误尝试", client_ip="192.168.3.60")


class AccessRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "access_devices.json"
        self.patcher = mock.patch.object(access_devices, "TOKEN_FILE", self.path)
        self.patcher.start()
        access_devices.reset_for_tests()

    def tearDown(self) -> None:
        self.patcher.stop()
        self._tmp.cleanup()

    def test_local_settings_can_create_code_and_list_delete_devices(self) -> None:
        from starlette.requests import Request
        from app.routers import access as access_router

        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/app/access/pairing",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8100),
            "scheme": "http",
            "query_string": b"",
        })
        with mock.patch.object(access_router.chat_access, "lan_enabled", return_value=True):
            pair = access_router.create_pairing(request)
        with mock.patch.object(access_router.chat_access, "lan_enabled", return_value=True), \
             mock.patch.object(access_router, "_default_lan_ip", return_value="192.168.3.4"):
            lan_pair = access_router.create_pairing(request)
        self.assertRegex(pair["code"], r"^\d{6}$")
        self.assertTrue(pair["url"].endswith("/pair"))
        self.assertEqual(pair["expires_in"], 300)
        self.assertEqual(lan_pair["url"], "http://192.168.3.4:8100/pair")

        code = lan_pair["code"]
        token, device = access_devices.pair_device(code, "厨房平板")
        self.assertTrue(token)

        with mock.patch.object(access_router.chat_access, "lan_enabled", return_value=True):
            listed = access_router.devices(request)
            removed = access_router.remove_device(device["id"], request)
        self.assertEqual(listed["devices"][0]["name"], "厨房平板")
        self.assertTrue(removed["success"])
        self.assertEqual(access_devices.list_devices(), [])

    def test_remote_device_management_is_rejected(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request
        from app.routers import access as access_router

        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/app/access/devices",
            "headers": [],
            "client": ("192.168.3.50", 50000),
            "server": ("192.168.3.4", 8100),
            "scheme": "http",
            "query_string": b"",
        })
        with mock.patch.object(access_router.chat_access, "lan_enabled", return_value=True):
            with self.assertRaises(HTTPException) as caught:
                access_router.devices(request)
        self.assertEqual(caught.exception.status_code, 403)

    def test_one_click_request_router_requires_local_approval(self) -> None:
        from fastapi import HTTPException
        from starlette.requests import Request
        from app.routers import access as access_router

        local = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/app/access/requests",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8100),
            "scheme": "http",
            "query_string": b"",
        })
        remote = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/app/access/requests",
            "headers": [(b"user-agent", b"Mozilla/5.0 (Linux; Android 14)")],
            "client": ("192.168.3.50", 50000),
            "server": ("192.168.3.4", 8100),
            "scheme": "http",
            "query_string": b"",
        })
        with mock.patch.object(access_router.chat_access, "lan_enabled", return_value=True), \
                mock.patch.object(access_router, "_default_lan_ip", return_value="192.168.3.4"):
            created = access_router.create_access_request(
                access_router.PairRequestCreate(device_name=""),
                remote,
            )
            info = access_router.connect_info(local)
            self.assertEqual(info["url"], "http://192.168.3.4:8100/web")

            with self.assertRaises(HTTPException) as caught:
                access_router.approve_access_request(created["request_id"], remote)
            self.assertEqual(caught.exception.status_code, 403)

            pending = access_router.claim_access_request(
                created["request_id"],
                access_router.PairRequestClaim(request_secret=created["request_secret"]),
                remote,
            )
            self.assertEqual(pending.status_code, 200)
            self.assertIn(b'"pending"', pending.body)

            approved = access_router.approve_access_request(created["request_id"], local)
            self.assertTrue(approved["success"])
            claimed = access_router.claim_access_request(
                created["request_id"],
                access_router.PairRequestClaim(request_secret=created["request_secret"]),
                remote,
            )
            self.assertEqual(claimed.status_code, 200)
            self.assertIn(b'"approved"', claimed.body)
            self.assertIn("pai_device_token=", claimed.headers.get("set-cookie", ""))


if __name__ == "__main__":
    unittest.main()
