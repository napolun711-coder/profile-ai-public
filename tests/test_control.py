"""单测（Phase 10.1）：手机 / 局域网的权限边界 —— 能续命、不能停服。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v

覆盖：
    local_addresses      本机地址集合（回环 + 网卡 IP），结果被缓存
    client_is_local      只有 127.0.0.1 / ::1 才算「本机页面」（手机不算）
    origin_allowed       来源校验：本机 / 本机网卡地址 / 外站，以及 allow_lan 开关的差别
    guard                停止类接口：手机（局域网）硬调一律 403
    guard_remote         续命 / 状态类接口：局域网放行，外站来源仍然 403
    session_state        手机页面看到 mode=lan、can_stop=false；本机页面 can_stop=true
    keepalive_delay      本机 25 秒 / 手机 180 秒（有作业在跑时都延长到 5 分钟）
    extend_stop          只推后「已预约的停止」；没有预约时什么都不做（手机制造不出停止）
    keepalive / leave / stop 接口：手机续命不新建预约、离开不预约、停服 403

全部离线执行：不发真实请求、不启动服务；定时器用完立刻取消，
另外把 exit_process / lifecycle 打进沙箱，万一有预约到期也不会真的停服。
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import Settings            # noqa: E402
from app.models.schemas import ServiceControl   # noqa: E402
from app.routers import control                 # noqa: E402

# 手机眼里的「电脑地址」：测试里当「别的设备的 IP」用（不是本机网卡地址）
LAN_IP = "192.168.1.23"


def make_request(host: str, origin: str = "") -> Request:
    """构造一个最小 HTTP 请求：只关心来源地址（client）与 Origin 头。"""
    headers = [(b"origin", origin.encode("utf-8"))] if origin else []
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/app/session",
        "headers": headers,
        "client": (host, 51234),
        "query_string": b"",
    })


def local_nic_ipv4() -> str:
    """本机的第一个非回环 IPv4（没有就跳过相关断言）。"""
    for addr in sorted(control.local_addresses()):
        if addr not in ("127.0.0.1", "localhost") and ":" not in addr:
            return addr
    return ""


class LocalAddressTest(unittest.TestCase):
    """本机地址集合：手机页面的 Origin 就是其中之一。"""

    def test_loopback_is_always_included(self):
        addrs = control.local_addresses()
        for expected in ("127.0.0.1", "::1", "localhost"):
            self.assertIn(expected, addrs)

    def test_result_is_cached(self):
        self.assertIs(control.local_addresses(), control.local_addresses())

    def test_local_hostnames_include_machine_name(self):
        hostname = socket.gethostname().lower()
        if not hostname:
            self.skipTest("取不到本机主机名")
        self.assertIn(hostname, control.local_hostnames())
        self.assertIn("localhost", control.local_hostnames())


class ClientHostTest(unittest.TestCase):
    """来源地址判定：本机 vs 手机。"""

    def test_loopback_clients_are_local(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(control.client_is_local(make_request(host)), host)

    def test_lan_client_is_not_local(self):
        self.assertFalse(control.client_is_local(make_request(LAN_IP)))


class OriginTest(unittest.TestCase):
    """来源校验：决定「谁能读状态 / 续命」，停止类接口保持严格。"""

    settings = Settings(app_host="0.0.0.0")

    def test_loopback_origin_allowed_in_both_modes(self):
        request = make_request("127.0.0.1", "http://127.0.0.1:8100/web")
        self.assertTrue(control.origin_allowed(request, self.settings))
        self.assertTrue(control.origin_allowed(request, self.settings, True))

    def test_missing_origin_is_left_to_other_checks(self):
        self.assertTrue(control.origin_allowed(make_request("127.0.0.1"), self.settings))

    def test_local_nic_address_needs_allow_lan(self):
        address = local_nic_ipv4()
        if not address:
            self.skipTest("本机没取到网卡 IPv4（离线环境），跳过")
        request = make_request(address, f"http://{address}:8100/web")
        self.assertFalse(control.origin_allowed(request, self.settings))
        self.assertTrue(control.origin_allowed(request, self.settings, True))

    def test_local_hostname_needs_allow_lan(self):
        # 平板也可能用「电脑名」打开：Origin 是主机名而不是 IP。
        # 续命 / 状态类接口认它；停止类接口仍然不认（保持严格）。
        hostname = socket.gethostname()
        if not hostname:
            self.skipTest("取不到本机主机名")
        request = make_request("127.0.0.1", f"http://{hostname}:8100/web")
        self.assertFalse(control.origin_allowed(request, self.settings))
        self.assertTrue(control.origin_allowed(request, self.settings, True))

    def test_local_hostname_is_case_insensitive(self):
        hostname = socket.gethostname()
        if not hostname:
            self.skipTest("取不到本机主机名")
        request = make_request("127.0.0.1", f"http://{hostname.upper()}:8100/web")
        self.assertTrue(control.origin_allowed(request, self.settings, True))

    def test_unknown_lan_origin_is_rejected(self):
        # 别的设备的 IP（不是本机地址）即使开着 allow_lan 也不认
        request = make_request(LAN_IP, f"http://{LAN_IP}:8100/web")
        self.assertFalse(control.origin_allowed(request, self.settings, True))

    def test_configured_app_host_still_allowed(self):
        settings = Settings(app_host=LAN_IP)
        request = make_request(LAN_IP, f"http://{LAN_IP}:8100/web")
        self.assertTrue(control.origin_allowed(request, settings))

    def test_foreign_origin_is_rejected(self):
        request = make_request("127.0.0.1", "http://example.com/web")
        self.assertFalse(control.origin_allowed(request, self.settings))
        self.assertFalse(control.origin_allowed(request, self.settings, True))


class GuardTest(unittest.TestCase):
    """三重校验：停止类严格，续命 / 状态类放宽但不放外站。"""

    settings = Settings(app_host="0.0.0.0")

    def setUp(self):
        patcher = mock.patch.object(control, "get_settings", lambda: self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_guard_rejects_lan_client(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard(make_request(LAN_IP))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_stop_guard_accepts_local_client_with_token(self):
        request = make_request("127.0.0.1", "http://127.0.0.1:8100/web")
        self.assertIs(control.guard(request, control.TOKEN), self.settings)

    def test_stop_guard_rejects_wrong_token(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard(make_request("127.0.0.1"), "wrong-token")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_remote_guard_allows_lan_client_without_token(self):
        self.assertIs(control.guard_remote(make_request(LAN_IP)), self.settings)

    def test_remote_guard_rejects_foreign_origin(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard_remote(make_request(LAN_IP, "http://example.com/web"))
        self.assertEqual(ctx.exception.status_code, 403)


class GuardActionTest(unittest.TestCase):
    """Phase 16：会动数据的动作（删除 / 打开文件夹 / 立即扫描）必须带令牌。

    与 GuardTest 的区别只有一条：**空令牌也算拒绝**。为什么重要 ——
    `guard` 把空令牌当成「没给就不验」，于是本机上任何一个脚本都能不带令牌删资料；
    这一档把令牌变成真正的第二因子。
    """

    settings = Settings(app_host="0.0.0.0")

    def setUp(self):
        patcher = mock.patch.object(control, "get_settings", lambda: self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_local_request_without_token_is_rejected(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard_action(make_request("127.0.0.1"))

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("令牌", str(ctx.exception.detail))

    def test_local_request_with_token_passes(self):
        request = make_request("127.0.0.1", "http://127.0.0.1:8100/files")

        self.assertIs(control.guard_action(request, control.TOKEN), self.settings)

    def test_lan_request_is_rejected_even_with_the_right_token(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard_action(make_request(LAN_IP), control.TOKEN)

        self.assertEqual(ctx.exception.status_code, 403)

    def test_foreign_origin_is_rejected(self):
        with self.assertRaises(control.HTTPException) as ctx:
            control.guard_action(
                make_request("127.0.0.1", "http://example.com/files"), control.TOKEN
            )

        self.assertEqual(ctx.exception.status_code, 403)


class SessionStateTest(unittest.TestCase):
    """GET /api/app/session：页面据此决定显示什么。"""

    settings = Settings(app_host="0.0.0.0")

    def call(self, host: str):
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control, "lifecycle_module", object), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            return control.session_state(make_request(host))

    def test_lan_page_cannot_stop_but_can_keepalive(self):
        body = self.call(LAN_IP)
        self.assertEqual(body["mode"], "lan")
        self.assertFalse(body["can_stop"])
        self.assertTrue(body["keepalive"])
        self.assertEqual(body["lan_delay_seconds"], self.settings.close_stop_lan_delay)
        self.assertNotIn("token", body)                 # 手机读不到令牌，所以停不了服务

    def test_local_page_can_stop(self):
        body = self.call("127.0.0.1")
        self.assertEqual(body["mode"], "local")
        self.assertTrue(body["can_stop"])

    def test_lan_keepalive_can_be_switched_off(self):
        self.settings = Settings(app_host="0.0.0.0", lan_keepalive=False)
        self.assertFalse(self.call(LAN_IP)["keepalive"])


class DelayTest(unittest.TestCase):
    """续命倒计时：本机 25 秒，手机 180 秒（忙时都延长）。"""

    settings = Settings(
        close_stop_delay=25, close_stop_lan_delay=180, close_stop_busy_delay=300
    )

    def test_local_and_lan(self):
        with mock.patch.object(control.ingest, "is_busy", lambda: False):
            self.assertEqual(control.keepalive_delay(self.settings, False), 25.0)
            self.assertEqual(control.keepalive_delay(self.settings, True), 180.0)

    def test_busy_extends_both(self):
        with mock.patch.object(control.ingest, "is_busy", lambda: True):
            self.assertEqual(control.keepalive_delay(self.settings, False), 300.0)
            self.assertEqual(control.keepalive_delay(self.settings, True), 300.0)


class ScheduleTest(unittest.TestCase):
    """预约与推后：手机只能推后、制造不出停止。"""

    settings = Settings(app_host="0.0.0.0")

    def setUp(self):
        control.cancel_stop()
        # 沙箱：万一有预约到期，也只是空转（不写日志、不杀进程、更不会真的停服）
        for name, replacement in (("exit_process", lambda: None),
                                  ("lifecycle_module", lambda: None)):
            patcher = mock.patch.object(control, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(control.cancel_stop)

    def test_extend_without_schedule_does_nothing(self):
        self.assertIsNone(control.extend_stop(30, "lan-keepalive"))
        self.assertFalse(control.scheduled())
        self.assertIsNone(control.pending_seconds())

    def test_extend_postpones_existing_schedule(self):
        control.schedule_stop(600, "tab-close")
        self.assertEqual(control.extend_stop(30, "lan-keepalive"), 30.0)
        self.assertTrue(control.scheduled())
        self.assertLessEqual(control.pending_seconds(), 30)
        self.assertGreater(control.pending_seconds(), 25)   # 确实被推后到 30 秒

    def test_extend_keeps_original_reason(self):
        control.schedule_stop(600, "tab-close")
        control.extend_stop(30)
        self.assertEqual(control._REASON, "tab-close")      # 日志里能看出是谁先提出停的

    def test_extend_appends_marker_once(self):
        control.schedule_stop(600, "tab-close")
        control.extend_stop(30, "lan-keepalive")
        self.assertEqual(control._REASON, "tab-close + lan-keepalive")
        control.extend_stop(30, "lan-keepalive")            # 重复续命不重复追加
        self.assertEqual(control._REASON, "tab-close + lan-keepalive")

    def test_lan_keepalive_never_creates_schedule(self):
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            body = control.keepalive(ServiceControl(), make_request(LAN_IP))
        self.assertEqual(body["status"], "alive")
        self.assertEqual(body["mode"], "lan")
        self.assertFalse(body["extended"])
        self.assertFalse(control.scheduled())               # 手机续命不会新建预约

    def test_lan_keepalive_postpones_pending_stop(self):
        control.schedule_stop(20, "tab-close")
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            body = control.keepalive(ServiceControl(), make_request(LAN_IP))
        self.assertTrue(body["extended"])
        self.assertGreater(control.pending_seconds(), 20)

    def test_lan_leave_is_ignored(self):
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            body = control.leave(ServiceControl(), make_request(LAN_IP))
        self.assertEqual(body["status"], "lan-ignored")
        self.assertFalse(control.scheduled())               # 手机离开不影响电脑上的服务

    def test_lan_stop_is_forbidden(self):
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            with self.assertRaises(control.HTTPException) as ctx:
                control.stop_now(ServiceControl(token=control.TOKEN), make_request(LAN_IP))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(control.scheduled())               # 手机停不掉服务

    def test_local_keepalive_cancels_schedule(self):
        control.schedule_stop(600, "tab-close")
        with mock.patch.object(control, "get_settings", lambda: self.settings), \
             mock.patch.object(control.ingest, "is_busy", lambda: False):
            body = control.keepalive(
                ServiceControl(token=control.TOKEN),
                make_request("127.0.0.1", "http://127.0.0.1:8100/web"),
            )
        self.assertEqual(body["status"], "alive")
        self.assertEqual(body["mode"], "local")
        self.assertFalse(control.scheduled())


if __name__ == "__main__":
    unittest.main()
