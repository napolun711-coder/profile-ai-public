"""单测（Phase 10）：服务启停的纯函数与停止流程（不真的杀进程）。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v

覆盖：
    iter_listening_pids          从 netstat 文本里挑出监听指定端口的进程
    port_from_url / *_port       URL 与 .env 里的端口解析
    command_belongs_to_project   哪些 python.exe 才算「本项目的进程」
    stop_service                 端口空闲 / 记录缺失 / 命令行不符 时的结论（用空闲端口，绝不误杀）
    stop_all                     只停本应用拉起的服务，并把结论写进 logs\\stop.log
    stop_qdrant                  docker 容器只停「本次由启动器拉起的」
    端口漂移（app.port）          start 用环境变量把服务起在别的端口时，停止/打开界面仍要认得
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lifecycle  # noqa: E402 - 被测模块在 scripts 目录（不是包）

# netstat -ano -p tcp 的真实输出形态（含 IPv6、ESTABLISHED、UDP 干扰行）
NETSTAT_SAMPLE = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:8100           0.0.0.0:0              LISTENING       1111
  TCP    127.0.0.1:9621         0.0.0.0:0              LISTENING       22484
  TCP    127.0.0.1:9621         127.0.0.1:52000        ESTABLISHED     22484
  TCP    [::]:9621              [::]:0                 LISTENING       22484
  TCP    127.0.0.1:6333         0.0.0.0:0              LISTENING       372
  UDP    127.0.0.1:11434        *:*                                    17872
"""


def free_port() -> int:
    """拿一个当前没人在听的端口（绑定后立刻释放）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class PortParsingTest(unittest.TestCase):
    """端口与 netstat 解析（纯函数）。"""

    def test_iter_listening_pids_reads_ipv4_and_ipv6(self):
        self.assertEqual(lifecycle.iter_listening_pids(NETSTAT_SAMPLE, 9621), [22484])
        self.assertEqual(lifecycle.iter_listening_pids(NETSTAT_SAMPLE, 8100), [1111])
        self.assertEqual(lifecycle.iter_listening_pids(NETSTAT_SAMPLE, 6333), [372])

    def test_iter_listening_pids_ignores_established_and_udp(self):
        # 11434 只有 UDP 行（Ollama 的 HTTP 端口是 TCP，这里刻意不给）
        self.assertEqual(lifecycle.iter_listening_pids(NETSTAT_SAMPLE, 11434), [])
        self.assertEqual(lifecycle.iter_listening_pids("", 8100), [])

    def test_port_from_url_accepts_common_forms(self):
        self.assertEqual(lifecycle.port_from_url("http://127.0.0.1:9621", 1), 9621)
        self.assertEqual(lifecycle.port_from_url("http://localhost:6333/", 1), 6333)
        self.assertEqual(lifecycle.port_from_url("127.0.0.1:8100", 1), 8100)
        self.assertEqual(lifecycle.port_from_url("[::1]:9621", 1), 9621)
        self.assertEqual(lifecycle.port_from_url("http://127.0.0.1", 9621), 9621)   # 没写端口
        self.assertEqual(lifecycle.port_from_url("", 9621), 9621)

    def test_env_port_helpers(self):
        env = {"APP_PORT": "8100", "LIGHTRAG_URL": "http://127.0.0.1:9621",
               "QDRANT_URL": "http://localhost:6333"}
        self.assertEqual(lifecycle.app_port(env), 8100)
        self.assertEqual(lifecycle.lightrag_port(env), 9621)
        self.assertEqual(lifecycle.qdrant_port(env), 6333)
        # PORT 别名与非法值容错
        self.assertEqual(lifecycle.app_port({"PORT": "8200"}), 8200)
        self.assertEqual(lifecycle.app_port({"APP_PORT": "abc"}), lifecycle.DEFAULT_APP_PORT)


class CommandLineTest(unittest.TestCase):
    """「这个 python.exe 是不是本项目的」判断（纯函数）。"""

    def test_matches_project_path(self):
        cmdline = '"' + str(ROOT) + '\\.venv\\Scripts\\python.exe" -m uvicorn app.main:app'
        self.assertTrue(lifecycle.command_belongs_to_project(cmdline, [str(ROOT), ROOT.name]))

    def test_matches_lightrag_names_and_forward_slashes(self):
        cmdline = "python.exe ./lightrag/lightrag_server.py --port 9621"
        self.assertTrue(lifecycle.command_belongs_to_project(cmdline, lifecycle.lightrag_hints({})))

    def test_rejects_other_python_processes(self):
        cmdline = '"C:\\other\\OpenWebUI\\.venv\\Scripts\\python.exe" -m uvicorn main:app'
        self.assertFalse(lifecycle.command_belongs_to_project(cmdline, [str(ROOT), ROOT.name]))
        self.assertFalse(lifecycle.command_belongs_to_project(cmdline, lifecycle.lightrag_hints({})))

    def test_empty_hints_accept_everything(self):
        self.assertTrue(lifecycle.command_belongs_to_project("anything", ()))


class StopServiceTest(unittest.TestCase):
    """停单个服务：重点验证「不该动手时绝不动手」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(lifecycle, "LOGS", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_port_free_reports_not_running(self):
        text = lifecycle.stop_service(
            lifecycle.APP_NAME, pid_file=self.logs / "app.pid", port=free_port(), hints=[str(ROOT)]
        )
        self.assertIn("未在运行", text)

    def test_stale_pid_file_is_cleaned(self):
        pid_file = self.logs / "app.pid"
        pid_file.write_text("999999", encoding="ascii")
        text = lifecycle.stop_service(
            lifecycle.APP_NAME, pid_file=pid_file, port=free_port(), hints=[str(ROOT)]
        )
        self.assertIn("已清理过期记录", text)
        self.assertFalse(pid_file.exists())

    def test_missing_record_and_no_port(self):
        text = lifecycle.stop_service(lifecycle.APP_NAME, pid_file=self.logs / "app.pid")
        self.assertIn("未找到进程记录", text)

    def test_port_owner_of_other_project_is_skipped(self):
        with mock.patch.object(lifecycle, "pids_on_port", lambda port: [4242]), \
             mock.patch.object(lifecycle, "query_process_image", lambda pid: "python.exe"), \
             mock.patch.object(lifecycle, "process_command_lines",
                               lambda pids: {4242: '"C:\\other\\python.exe" -m uvicorn main:app'}), \
             mock.patch.object(lifecycle, "kill_pid") as killer:
            text = lifecycle.stop_service(
                lifecycle.APP_NAME, pid_file=self.logs / "app.pid", port=8100, hints=[str(ROOT)]
            )
        self.assertIn("已跳过", text)
        killer.assert_not_called()

    def test_port_owner_of_this_project_is_killed(self):
        cmdline = '"' + str(ROOT / ".venv" / "Scripts" / "python.exe") + '" -m uvicorn app.main:app'
        with mock.patch.object(lifecycle, "pids_on_port", lambda port: [4242]), \
             mock.patch.object(lifecycle, "query_process_image", lambda pid: "python.exe"), \
             mock.patch.object(lifecycle, "process_command_lines", lambda pids: {4242: cmdline}), \
             mock.patch.object(lifecycle, "process_parents", lambda pids: {4242: 0}), \
             mock.patch.object(lifecycle, "kill_pid", return_value=True) as killer:
            text = lifecycle.stop_service(
                lifecycle.APP_NAME, pid_file=self.logs / "app.pid", port=8100, hints=[str(ROOT), ROOT.name]
            )
        self.assertIn("已停止", text)
        killer.assert_called_once_with(4242)


class StopAllTest(unittest.TestCase):
    """整体停止：只停本应用拉起的服务，并留下 logs\\stop.log。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _patch(self):
        return [
            mock.patch.object(lifecycle, "LOGS", self.logs),
            mock.patch.object(lifecycle, "APP_PID_FILE", self.logs / "app.pid"),
            mock.patch.object(lifecycle, "LIGHTRAG_PID_FILE", self.logs / "lightrag.pid"),
            mock.patch.object(lifecycle, "QDRANT_PID_FILE", self.logs / "qdrant.pid"),
            mock.patch.object(lifecycle, "QDRANT_MARKER", self.logs / "qdrant.started"),
            mock.patch.object(lifecycle, "STOP_LOG", self.logs / "stop.log"),
            mock.patch.object(lifecycle, "APP_PORT_FILE", self.logs / "app.port"),
            mock.patch.object(lifecycle, "pids_on_port", lambda port: []),
            mock.patch.object(lifecycle, "port_open", lambda host, port, timeout=1.5: False),
            mock.patch.object(lifecycle, "wait_port_closed", lambda host, port, timeout=8.0: True),
        ]

    def test_stop_all_leaves_foreign_qdrant_alone(self):
        patchers = self._patch()
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        messages, problems = lifecycle.stop_all(reason="unittest")

        joined = " ".join(messages)
        self.assertIn("本应用：未在运行", joined)
        self.assertIn("知识库（LightRAG）：未在运行", joined)
        self.assertIn("不是本应用拉起的", joined)       # 共享 Qdrant 不动
        self.assertEqual(problems, [])
        self.assertTrue((self.logs / "stop.log").is_file())
        self.assertIn("unittest", (self.logs / "stop.log").read_text(encoding="utf-8"))

    def test_stop_all_defers_app_when_requested_from_web(self):
        patchers = self._patch()
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.logs / "app.pid").write_text("4242", encoding="ascii")

        messages, _ = lifecycle.stop_all(reason="web-leave", defer_app=True)
        joined = " ".join(messages)
        self.assertIn("将随本次请求退出", joined)
        self.assertIn("4242", joined)
        self.assertFalse((self.logs / "app.pid").exists())    # 记录已清理


class RuntimePortTest(unittest.TestCase):
    """端口漂移：启动器实际用的端口（logs\\app.port）停止与打开界面也要认。

    实测事故：系统环境变量 APP_PORT=8137（优先级高于 .env）让 start 把服务起在 8137，
    而停止只读 .env 的 8100 —— logs\\stop.log 写「本应用：未在运行」，服务却还在跑，
    「打开界面」也打不开。启动器现在会把实际端口写进 logs\\app.port。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.port_file = self.logs / "app.port"
        patcher = mock.patch.object(lifecycle, "APP_PORT_FILE", self.port_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_logs(self):
        """把停止流程用到的记录文件全指到临时目录（绝不碰真机 logs\\）。"""
        return [
            mock.patch.object(lifecycle, "LOGS", self.logs),
            mock.patch.object(lifecycle, "APP_PID_FILE", self.logs / "app.pid"),
            mock.patch.object(lifecycle, "LIGHTRAG_PID_FILE", self.logs / "lightrag.pid"),
            mock.patch.object(lifecycle, "QDRANT_PID_FILE", self.logs / "qdrant.pid"),
            mock.patch.object(lifecycle, "QDRANT_MARKER", self.logs / "qdrant.started"),
            mock.patch.object(lifecycle, "STOP_LOG", self.logs / "stop.log"),
            mock.patch.object(lifecycle, "read_env", lambda path=None: {
                "APP_PORT": "8100", "LIGHTRAG_URL": "http://127.0.0.1:9621",
            }),
            mock.patch.object(lifecycle, "wait_port_closed", lambda host, port, timeout=8.0: True),
        ]

    def test_read_port_record_tolerates_bad_content(self):
        self.assertEqual(lifecycle.read_port_record(), 0)                # 没有记录
        self.port_file.write_text("", encoding="ascii")
        self.assertEqual(lifecycle.read_port_record(), 0)
        self.port_file.write_text("随便写的", encoding="utf-8")
        self.assertEqual(lifecycle.read_port_record(), 0)
        self.port_file.write_text("70000", encoding="ascii")             # 超出端口范围
        self.assertEqual(lifecycle.read_port_record(), 0)
        self.port_file.write_text("8137", encoding="ascii")
        self.assertEqual(lifecycle.read_port_record(), 8137)

    def test_candidates_put_recorded_port_first(self):
        env = {"APP_PORT": "8100"}
        self.assertEqual(lifecycle.app_port_candidates(env), [8100])           # 没记录 → 只用配置端口
        self.port_file.write_text("8137", encoding="ascii")
        self.assertEqual(lifecycle.app_port_candidates(env), [8137, 8100])     # 实际端口优先
        self.port_file.write_text("8100", encoding="ascii")                    # 与配置相同 → 去重
        self.assertEqual(lifecycle.app_port_candidates(env), [8100])


    def test_process_env_overrides_dotenv(self):
        """与 app/core/config.py 一致：系统环境变量 > .env > 默认值（本次错配的根因）。"""
        dotenv = {"APP_PORT": "8100"}
        with mock.patch.object(lifecycle, "read_env", lambda path=None: dotenv), \
             mock.patch.dict(os.environ, {"APP_PORT": "8137"}, clear=True):
            self.assertEqual(lifecycle.app_port(), 8137)
        with mock.patch.object(lifecycle, "read_env", lambda path=None: dotenv), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(lifecycle.app_port(), 8100)

    def test_running_app_port_prefers_listening_candidate(self):
        self.port_file.write_text("8137", encoding="ascii")
        env = {"APP_PORT": "8100"}
        with mock.patch.object(lifecycle, "port_open", lambda host, port, timeout=1.5: port == 8137):
            self.assertEqual(lifecycle.running_app_port(env), 8137)
        with mock.patch.object(lifecycle, "port_open", lambda host, port, timeout=1.5: port == 8100):
            self.assertEqual(lifecycle.running_app_port(env), 8100)      # 服务回到了配置端口
        with mock.patch.object(lifecycle, "port_open", lambda host, port, timeout=1.5: False):
            self.assertEqual(lifecycle.running_app_port(env), 8100)      # 都没在听 → 配置端口

    def test_stop_all_stops_app_on_recorded_port(self):
        """配置端口空闲、实际端口上跑着本应用 → 必须停在那个实际端口上。"""
        self.port_file.write_text("8137", encoding="ascii")
        cmdline = ('"' + str(ROOT / ".venv" / "Scripts" / "python.exe")
                   + '" -m uvicorn main:app --host 127.0.0.1 --port 8137')
        killer = mock.Mock(return_value=True)
        patchers = self._patch_logs() + [
            mock.patch.object(lifecycle, "pids_on_port", lambda port: [4242] if port == 8137 else []),
            mock.patch.object(lifecycle, "query_process_image", lambda pid: "python.exe"),
            mock.patch.object(lifecycle, "process_command_lines", lambda pids: {4242: cmdline}),
            mock.patch.object(lifecycle, "process_parents", lambda pids: {4242: 0}),
            mock.patch.object(lifecycle, "kill_pid", killer),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        messages, problems = lifecycle.stop_all(reason="unittest-port-drift")

        joined = " ".join(messages)
        self.assertIn("本应用：已停止", joined)
        self.assertIn("4242", joined)
        killer.assert_called_once_with(4242)
        self.assertEqual(problems, [])
        self.assertFalse(self.port_file.exists())        # 记录用完即删，避免下次指向旧端口

    def test_stop_all_never_kills_foreign_owner_of_configured_port(self):
        """实际端口没在听、配置端口被别人占着 → 只报告，不误杀。"""
        self.port_file.write_text("8137", encoding="ascii")
        killer = mock.Mock(return_value=True)
        patchers = self._patch_logs() + [
            mock.patch.object(lifecycle, "pids_on_port", lambda port: [4242] if port == 8100 else []),
            mock.patch.object(lifecycle, "query_process_image", lambda pid: "python.exe"),
            mock.patch.object(lifecycle, "process_command_lines",
                              lambda pids: {4242: '"C:\\\\other\\\\python.exe" -m uvicorn main:app'}),
            mock.patch.object(lifecycle, "kill_pid", killer),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        messages, _ = lifecycle.stop_all(reason="unittest-foreign-port")

        self.assertIn("已跳过", " ".join(messages))
        killer.assert_not_called()


class StopQdrantTest(unittest.TestCase):
    """Qdrant 只在「本次由启动器拉起」时才停。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(lifecycle, "LOGS", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_docker_container_started_by_us_is_stopped(self):
        marker = self.logs / "qdrant.started"
        marker.write_text("docker:qdrant", encoding="utf-8")
        done = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(lifecycle, "QDRANT_MARKER", marker), \
             mock.patch.object(lifecycle.subprocess, "run", return_value=done):
            text = lifecycle.stop_qdrant({})
        self.assertIn("已停止容器 qdrant", text)
        self.assertFalse(marker.exists())

    def test_externally_started_container_is_kept(self):
        text = lifecycle.stop_qdrant({})
        self.assertIn("不是本应用拉起的", text)


if __name__ == "__main__":
    unittest.main()

