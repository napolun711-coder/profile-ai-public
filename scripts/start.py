"""本地知识库助手 - 一键启动（Phase 4.1）。

做的事情：
  1. 读取项目 .env 配置（系统环境变量优先）
  2. 运行环境自检（scripts/check_env.ps1）
  3. 按顺序拉起 LightRAG 知识库服务 与 本应用
  4. 屏幕中央显示一条启动进度（scripts/splash.py，独立进程，可 Esc 关掉）
  5. 等待就绪后自动打开浏览器

用法（推荐静默）：双击项目根目录的 start_silent.vbs —— 全程不出现任何命令行窗口；
用法（前台调试）：双击 start.bat，逐步输出可见；
用法（命令行）：.venv\\Scripts\\python.exe scripts\\start.py

静默运行会把完整输出写入用户数据目录下的 logs/start.log；被拉起的服务自身不带窗口，
输出分别写入 logs/lightrag_console.txt 与 logs/app_console.txt。启动失败时会额外生成
logs/startup-diagnostic.txt，汇总进程状态、端口监听和相关日志末尾。

可选参数：
  --skip-browser        不自动打开浏览器
  --ready-timeout 180   等待 LightRAG 就绪的最长秒数（默认 180）
  --app-ready-timeout 180  等待本应用就绪的最长秒数（默认 180，可用 APP_READY_TIMEOUT 覆盖）
  --force               即使自检发现致命问题也继续启动
  --silent              静默模式：不等待回车，失败时弹 Windows 消息框
  --no-pause            结束时不等回车（由脚本或快捷方式调用时使用）
  --no-splash           不显示启动进度窗（配置项 START_SPLASH=false 同效）

退出码：0 = 启动完成；3 = 前置条件不满足
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

try:
    import requests
except ImportError:
    print("缺少 requests 依赖，请先在项目目录执行：uv sync")
    sys.exit(3)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

# Reuse the app path/config model (single source of truth).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core import user_settings  # noqa: E402
from app.core import runtime as runtime_state  # noqa: E402
from app.core.paths import (  # noqa: E402
    LIGHTRAG_HOME,
    LIGHTRAG_INPUT_DIR,
    LOG_DIR,
    PROFILE_AI_HOME,
    RAG_STORAGE_DIR,
    ensure_directories,
)

LOGS = LOG_DIR

# 进度窗通信（scripts\splash_state.py，与启动器同目录）
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import splash_state  # noqa: E402

STARTED = []
DEGRADED = []
OPEN_HANDLES = []
FAILED = False
QUOTE_CHARS = (chr(34), chr(39))

# 静默模式（由 start_silent.vbs 传入 --silent）：没有控制台可看，排查全靠日志
SILENT = "--silent" in sys.argv
LOG_FILE = LOGS / "start.log"
DIAGNOSTIC_FILE = LOGS / "startup-diagnostic.txt"

# 启动进度窗（Phase 11）：本脚本只负责「写进度 + 拉起/关闭窗口」，窗口本身是独立进程。
# 它崩了、被 Esc 关掉、被杀掉，都不会影响启动 —— 见 scripts\splash.py 顶部的说明。
PROGRESS = None              # splash_state.Progress 实例（main() 里创建）
SPLASH_PROCESS = None        # 进度窗进程句柄
SPLASH_PID_FILE = LOGS / "splash.pid"


def read_env(path: Path) -> dict:
    """读取 KEY=VALUE 形式的 .env 文件（忽略注释、去成对引号）。"""
    data = {}
    if not path.is_file():
        return data
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTE_CHARS:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def cfg(env: dict, key: str, default: str = "") -> str:
    """取配置：系统环境变量 > .env > 默认值。"""
    return os.environ.get(key) or env.get(key) or default


def step(text: str) -> None:
    print("")
    print("  " + text)


def done(text: str) -> None:
    print("  [启动成功] " + text)


def skip(text: str) -> None:
    print("  [已在运行] " + text)


def warn(text: str, hint: str = "") -> None:
    print("  [注意] " + text)
    if hint:
        print("         建议：" + hint)


def error(text: str, hint: str = "") -> None:
    print("  [失败] " + text)
    if hint:
        print("         建议：" + hint)


def notify_error(title: str, text: str) -> None:
    """静默模式下用 Windows 消息框报错（用户看不到控制台）；前台模式不做任何事。"""
    if not SILENT:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001 - 非 Windows 或消息框不可用时不阻断退出
        pass


class _Tee:
    """把启动器输出同时写到控制台与 logs/start.log（静默时日志是唯一排查入口）。"""

    def __init__(self, handle) -> None:
        self._handle = handle

    def write(self, text: str):
        console = sys.__stdout__
        if console is not None:
            try:
                console.write(text)
                console.flush()
            except (OSError, ValueError):
                pass
        self._handle.write(text)
        self._handle.flush()
        return len(text)

    def flush(self) -> None:
        try:
            self._handle.flush()
        except (OSError, ValueError):
            pass


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """TCP 端口是否可连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_bindable(host: str, port: int) -> bool:
    """本机能否绑定该端口；用于识别 Windows 保留端口导致的 WinError 10013。"""
    probe_host = "127.0.0.1" if host in ("localhost", "0.0.0.0", "::", "") else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((probe_host, int(port)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def choose_lightrag_port(host: str, preferred: int, app_port: int) -> int:
    """为本地 LightRAG 选择可绑定端口；优先尝试相邻且常见的安全端口。"""
    candidates = [9961, 9001, 9101, 9201, 9301, 9401, 9501, 12021, 18021]
    for port in candidates:
        if port == int(app_port):
            continue
        if not port_open(host, port) and port_bindable(host, port):
            return port
    return 0


def default_lan_ip() -> str:
    """当前联网网卡的 IPv4（给平板 / 手机用的那个地址）。

    为什么不用 socket.gethostname()：这台机器常有 VMware / VirtualBox / WSL 的虚拟网卡，
    主机名解析可能先返回 192.168.56.1 之类**别人连不到**的地址。用「默认出口路由」问一次
    操作系统（UDP connect 不会真的发包），拿到的一定是当前真正联网的网卡。
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = str(probe.getsockname()[0] or "")
        finally:
            probe.close()
        if ip and not ip.startswith("127.") and ip != "0.0.0.0":
            return ip
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0] or "")
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
    except OSError:
        pass
    return ""


def split_url(url: str, default_port: int):
    # 从 URL 中取出主机与端口
    target = url.strip()
    if "://" not in target:
        target = "http://" + target
    rest = target.split("://", 1)[1]
    rest = rest.split("/", 1)[0]
    if ":" in rest:
        host, _, port_text = rest.partition(":")
        try:
            return host, int(port_text)
        except ValueError:
            return host, default_port
    return rest, default_port


def wait_http(url: str, timeout_seconds: int, label: str, request_timeout: int = 3,
              progress_range=None, detail: str = "", process=None, accept=None) -> bool:
    # 轮询直到 URL 可访问，或超时；request_timeout 是单次请求超时
    # （/health 在依赖不可用时要探测多个下游，可能耗时 4~6 秒，故可单独放宽）
    # progress_range=(起点%, 终点%)：等待期间让进度条「渐近爬升」，一直动但绝不到 100%
    # （真正就绪才写 100%，见 scripts\splash_state.py 的 Progress.creep）。
    # process 与 accept 用于「本应用」：进程一退出立即失败；JSON 也必须确认是本应用。
    started = time.time()
    deadline = started + timeout_seconds
    tick = 0
    while time.time() < deadline:
        if process is not None:
            exit_code = process.poll()
            if exit_code is not None:
                print("  [失败] " + label + " 启动进程已退出（退出码 " + str(exit_code) + "）")
                return False
        try:
            response = requests.get(url, timeout=request_timeout)
            if response.status_code < 500:
                if accept is None:
                    return True
                try:
                    if accept(response):
                        return True
                except (AttributeError, TypeError, ValueError):
                    pass
        except requests.RequestException:
            pass
        if progress_range and PROGRESS is not None:
            elapsed = time.time() - started
            wait_detail = detail
            if elapsed >= 20:
                wait_detail = (detail or (label + " 就绪中")) + \
                    "（已等待 " + str(int(elapsed)) + " 秒）"
            PROGRESS.creep(progress_range[0], progress_range[1], time.time() - started,
                           wait_detail)
        time.sleep(1.5)
        tick += 1
        if tick % 4 == 0:
            print("         等待 " + label + " 就绪（" + str(int(time.time() - started)) +
                  "/" + str(timeout_seconds) + " 秒）...")
    return False


def app_live_ok(response) -> bool:
    """只接受本应用的轻量存活响应，避免把端口上的其他服务当成已启动。"""
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("app") == "ok"

def tail_lines(path: Path, count: int):
    # 读取文件末尾若干非空行（用于展示启动失败原因）
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in content.splitlines() if line.strip()][-count:]

def is_our_app(health_url: str) -> bool:
    # 确认端口上运行的确实是本应用（避免把其他 HTTP 服务误判为已在运行）
    try:
        payload = requests.get(health_url, timeout=5).json()
    except (requests.RequestException, ValueError):
        return False
    return payload.get("app") == "ok"

def save_port(port: int) -> None:
    """记录本次实际使用的端口（logs\\app.port），供「停止 / 打开界面」使用（Phase 16.2）。

    为什么要记：端口不总等于 .env 里的 APP_PORT —— 系统环境变量优先级更高（见 .clinerules 的
    配置读取顺序），也可能因占用而变化。停止一侧若只按 .env 找，就会出现实测过的怪象：
    stop.log 写「本应用：未在运行」，而服务其实还在跑（停不掉、界面也打不开）。
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / "app.port").write_text(str(port), encoding="ascii")


def write_startup_diagnostic(app_port: int, process=None) -> Path:
    """把启动失败现场写入一个可直接发送给维护者的文本文件。"""
    lines = [
        "ProfileAI 启动诊断",
        "生成时间：" + time.strftime("%Y-%m-%d %H:%M:%S"),
        "项目目录：" + str(ROOT),
        "用户数据：" + str(PROFILE_AI_HOME),
        "应用端口：" + str(app_port),
        "系统：" + platform.platform(),
        "Python：" + sys.version.replace("\n", " "),
    ]
    if process is not None:
        exit_code = process.poll()
        lines.extend([
            "应用进程 PID：" + str(process.pid),
            "应用进程状态：" + ("运行中" if exit_code is None else "已退出（退出码 " + str(exit_code) + "）"),
        ])

    runtime_file = PROFILE_AI_HOME / "config" / "runtime.json"
    try:
        runtime_text = runtime_file.read_text(encoding="utf-8").strip()
    except OSError:
        runtime_text = ""
    lines.extend(["", "runtime.json：", runtime_text or "（不存在或不可读）"])

    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            timeout=10,
            creationflags=no_window_flags(),
        )
        output = decode_console_bytes(netstat.stdout or b"")
        listeners = [
            line.strip() for line in output.splitlines()
            if ":" + str(app_port) in line and "LISTENING" in line.upper()
        ]
    except (OSError, subprocess.SubprocessError):
        listeners = []
    lines.extend(["", "应用端口监听："])
    lines.extend(listeners or ["（没有发现监听进程）"])

    log_files = (
        ("app_console.txt", LOGS / "app_console.txt", 80),
        ("app.log", LOGS / "app.log", 80),
        ("start.log", LOG_FILE, 80),
        ("lightrag_console.txt", LOGS / "lightrag_console.txt", 40),
    )
    for label, path, count in log_files:
        lines.extend(["", "===== " + label + "（末尾）====="])
        recent = tail_lines(path, count)
        lines.extend(recent or ["（无内容或文件不存在）"])

    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        DIAGNOSTIC_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("  启动诊断：" + str(DIAGNOSTIC_FILE))
    except OSError as exc:
        print("  [注意] 无法写入启动诊断：" + str(exc))
    return DIAGNOSTIC_FILE


def save_pid(name: str, pid: int) -> None:
    """记录进程号，供「停止.bat」使用。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / (name + ".pid")).write_text(str(pid), encoding="ascii")


def save_marker(name: str, value: str) -> None:
    """记录「这个服务是本次启动器拉起的」（Phase 10）。

    docker 拉起的容器没有 pid 可记，因此另记一个标记文件；停止时据此判断要不要连容器一起停。
    别人先启好的共享容器（例如与 Dify 共用）绝不能由我们停掉，所以没有标记 = 一律不动。
    """
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / (name + ".started")).write_text(value, encoding="utf-8")


def spawn(exe: str, args, cwd: Path, name: str, extra_env=None):
    """启动外部进程（无控制台窗口）并记录 PID；输出写入 logs/<名字>_console.txt。"""
    child = dict(os.environ)
    # 输出被重定向到文件时 Python 会退化为 GBK 编码（LightRAG 曾因此启动即崩溃），
    # 这里统一强制子进程使用 UTF-8。
    child["PYTHONIOENCODING"] = "utf-8"
    child["PYTHONUTF8"] = "1"
    # 重定向到文件时 Python 默认块缓冲，日志要等缓冲区满才落盘；静默模式全靠日志排查，
    # 因此强制子进程不缓冲，日志能实时写入。
    child["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        child.update(extra_env)
    LOGS.mkdir(parents=True, exist_ok=True)
    handle = open(LOGS / (name + "_console.txt"), "w", encoding="utf-8", errors="replace")
    OPEN_HANDLES.append(handle)
    process = subprocess.Popen(
        [exe] + list(args), cwd=str(cwd), env=child,
        stdout=handle, stderr=subprocess.STDOUT, creationflags=spawn_flags(),
    )
    save_pid(name, process.pid)
    STARTED.append((name, process.pid))
    return process


def run_check_env() -> int:
    """调用 PowerShell 环境自检脚本，返回其退出码。"""
    script = SCRIPTS / "check_env.ps1"
    if not script.is_file():
        warn("未找到环境自检脚本，已跳过", str(script))
        return 0
    try:
        if SILENT:
            # 静默模式：PowerShell 自己也会开一个窗口，这里无窗口运行并把输出收进日志
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, creationflags=no_window_flags(),
            )
            body = decode_console_bytes((result.stdout or b"") + (result.stderr or b"")).rstrip()
            if body:
                print(body)
        else:
            # 前台模式：沿用「继承当前窗口」的方式，保留自检脚本的彩色输出
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
            )
        return result.returncode
    except OSError as exc:
        warn("环境自检无法执行（" + exc.__class__.__name__ + "）", "可稍后手动运行 scripts/check_env.ps1")
        return 0

def pause(prompt: str) -> None:
    """等待用户回车（静默模式或非交互环境下直接跳过）。"""
    if SILENT:
        return
    try:
        input(prompt)
    except EOFError:
        pass


def no_window_flags():
    """Windows 下不创建控制台窗口；给短命令（tasklist/taskkill/netstat/docker/自检）用。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def decode_console_bytes(raw: bytes) -> str:
    """解码控制台子进程的字节流。

    PowerShell 5.1 输出被重定向时用的是本机 OEM 编码（简体中文为 GBK），而部分脚本会
    主动改成 UTF-8；这里两种都试一遍，避免在日志里出现乱码。
    """
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def spawn_flags():
    """被拉起的服务同样不带窗口（日志见 logs/<名字>_console.txt）。"""
    return no_window_flags()


# ============================================================
#  启动进度窗（Phase 11）
# ============================================================

def splash_enabled(env: dict) -> bool:
    """是否显示启动进度窗：命令行 --no-splash > 环境变量 START_SPLASH > 默认开。"""
    if "--no-splash" in sys.argv:
        return False
    value = cfg(env, "START_SPLASH", "true").strip().lower()
    return value not in ("0", "false", "no", "off")


def splash_python() -> str:
    """拉起进度窗用哪个解释器。

    优先 sys._base_executable 而不是 sys.executable：uv 生成的 .venv\\Scripts\\python.exe 是个
    「转发壳」，真正的解释器是它的子进程 —— 用壳拉起时 Popen 拿到的是壳的 PID，
    壳一退出就再也找不到那个窗口进程了（收尾会留下关不掉的孤儿窗）。
    进度窗只用标准库 + tkinter，不需要 .venv 里的第三方包，所以直接用基础解释器最省事：
    一个进程 = 一个窗口 = 一个 PID。
    （注意：被拉起的本应用 / LightRAG 仍必须用 .venv 的解释器，那里才有依赖。）
    """
    base = getattr(sys, "_base_executable", "") or ""
    if base and Path(base).is_file():
        return base
    return sys.executable


def spawn_splash() -> None:
    """拉起进度窗（无窗口方式；窗口由它自己画，日志写 logs\\splash.log）。"""
    global SPLASH_PROCESS
    script = SCRIPTS / "splash.py"
    if not script.is_file():
        print("  [注意] 未找到进度窗脚本，已跳过（不影响启动）")
        return
    try:
        SPLASH_PROCESS = subprocess.Popen(
            [splash_python(), str(script), "--owner-pid", str(os.getpid())],
            cwd=str(ROOT), creationflags=spawn_flags(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        SPLASH_PROCESS = None
        print("  [注意] 进度窗无法启动（" + exc.__class__.__name__ + "），已跳过（不影响启动）")


def read_pid_file(path: Path):
    """读 PID 文件；不存在或内容不是数字都返回 None。"""
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def close_splash(wait_seconds: float = 1.5) -> None:
    """关闭进度窗：先请它自己淡出，不听话再兜底强杀。

    强杀是「宁可多杀一次」的保险：进度窗正常退出会删掉 logs\\splash.pid，
    所以「文件还在」就等于「还活着」。它要是赖在屏幕上，用户会以为启动卡死了。
    """
    global SPLASH_PROCESS
    if PROGRESS is not None and PROGRESS.enabled:
        PROGRESS.closing()
    process, SPLASH_PROCESS = SPLASH_PROCESS, None
    deadline = time.time() + max(0.0, wait_seconds)
    while process is not None and process.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
    pid = read_pid_file(SPLASH_PID_FILE)
    if pid and pid != os.getpid():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           text=True, timeout=20, creationflags=no_window_flags())
            print("  [注意] 进度窗未自行退出，已强制关闭（进程 " + str(pid) + "）")
        except (OSError, subprocess.SubprocessError):
            pass


def milestone(percent: float, text: str) -> None:
    """打一个步骤标题，同时把进度推给进度窗（两边永远一致）。"""
    step(text)
    if PROGRESS is not None:
        PROGRESS.set(percent, text)


def progress_failed() -> int:
    """启动失败的统一收尾：进度窗变红 → 停 1.5 秒让人看清 → 关窗，返回退出码 3。

    顺序很关键：必须先把红字显示出来再关窗，否则用户只看到「窗一闪就没了」，
    比不给提示还糟。弹消息框（notify_error）由 __main__ 在 main() 返回之后做。
    """
    if PROGRESS is not None and PROGRESS.enabled:
        detail = (
            "诊断：" + str(DIAGNOSTIC_FILE)
            if DIAGNOSTIC_FILE.is_file()
            else "日志：" + str(LOG_FILE)
        )
        PROGRESS.fail(detail)
        time.sleep(1.5)
    close_splash(wait_seconds=1.0)
    return 3


def main() -> int:
    global FAILED

    # 输出同时写「控制台」与 logs/start.log：静默模式（VBS 入口）下控制台不可见
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        log_handle = open(LOG_FILE, "w", encoding="utf-8", errors="replace")
        OPEN_HANDLES.append(log_handle)
        sys.stdout = _Tee(log_handle)
    except OSError:
        pass

    skip_browser = "--skip-browser" in sys.argv
    force = "--force" in sys.argv
    no_pause = "--no-pause" in sys.argv
    ready_timeout = 180
    app_ready_timeout = 180
    if "--ready-timeout" in sys.argv:
        index = sys.argv.index("--ready-timeout")
        if index + 1 < len(sys.argv):
            try:
                ready_timeout = int(sys.argv[index + 1])
            except ValueError:
                pass
    if "--app-ready-timeout" in sys.argv:
        index = sys.argv.index("--app-ready-timeout")
        if index + 1 < len(sys.argv):
            try:
                app_ready_timeout = int(sys.argv[index + 1])
            except ValueError:
                pass

    env = read_env(ROOT / ".env")
    if "--app-ready-timeout" not in sys.argv:
        try:
            app_ready_timeout = int(cfg(env, "APP_READY_TIMEOUT", "180"))
        except ValueError:
            app_ready_timeout = 180
    app_ready_timeout = max(30, min(900, app_ready_timeout))

    app_host = cfg(env, "APP_HOST", cfg(env, "HOST", "127.0.0.1"))
    app_port = int(cfg(env, "APP_PORT", cfg(env, "PORT", "8100")))
    ensure_directories()
    configured_lightrag_url = cfg(env, "LIGHTRAG_URL", "").strip()
    lightrag_url = configured_lightrag_url or runtime_state.persisted_lightrag_url() or "http://127.0.0.1:9961"
    # ---- Phase 4.2：读取网页设置页保存的用户配置（config/user-settings.json）----
    user_conf = user_settings.load()
    lightrag_env = user_settings.to_env(user_conf, "lightrag")
    app_env = user_settings.to_env(user_conf, "app")

    # Relative LightRAG paths are resolved against the profile data root.
    for rel_key in ("WORKING_DIR", "INPUT_DIR"):
        rel_value = lightrag_env.get(rel_key)
        if rel_value and not Path(rel_value).is_absolute():
            lightrag_env[rel_key] = str((PROFILE_AI_HOME / rel_value).resolve())

    lightrag_home_value = (
        os.environ.get("LIGHTRAG_HOME")
        or str(user_conf.get("paths", {}).get("lightrag_home") or "")
        or cfg(env, "LIGHTRAG_HOME", "")
        or str(LIGHTRAG_HOME)
    )
    lightrag_home = Path(lightrag_home_value)
    if not lightrag_home.is_absolute():
        lightrag_home = (PROFILE_AI_HOME / lightrag_home).resolve()
    lightrag_default_exe = lightrag_home / ".venv" / "Scripts" / "lightrag-server.exe"
    lightrag_exe = Path(cfg(env, "LIGHTRAG_SERVER_EXE", str(lightrag_default_exe)))
    ollama_url = cfg(env, "OLLAMA_URL", "http://localhost:11434")
    qdrant_url = cfg(env, "QDRANT_URL", "http://localhost:6333")
    qdrant_enabled = cfg(env, "QDRANT_ENABLED", "auto").strip().lower()
    qdrant_exe = cfg(env, "QDRANT_EXE", "")

    ollama_host, ollama_port = split_url(ollama_url, 11434)
    qdrant_host, qdrant_port = split_url(qdrant_url, 6333)
    try:
        selected_url, changed = runtime_state.resolve_lightrag_endpoint(
            lightrag_url, app_port, prefer_persisted=not bool(configured_lightrag_url)
        )
    except RuntimeError as exc:
        error("无法为 LightRAG 选择可用端口", str(exc))
        return progress_failed()
    if changed:
        print("  [修复] LightRAG 端口不可用或服务不健康，本次使用 " + selected_url)
    lightrag_url = selected_url
    lr_host, lr_port = runtime_state.split_url(lightrag_url)
    app_env["LIGHTRAG_URL"] = lightrag_url
    lightrag_env.setdefault("WORKING_DIR", str(RAG_STORAGE_DIR))
    lightrag_env.setdefault("INPUT_DIR", str(LIGHTRAG_INPUT_DIR))

    python_exe = ROOT / ".venv" / "Scripts" / "python.exe"
    uvicorn_exe = ROOT / ".venv" / "Scripts" / "uvicorn.exe"
    browser_host = "127.0.0.1" if app_host in ("0.0.0.0", "") else app_host
    app_root = "http://" + browser_host + ":" + str(app_port)
    app_health = app_root + "/health"
    app_live = app_root + "/health/live"

    print("")
    print("  ============================================================")
    print("   本地知识库助手 - 一键启动")
    print("  ============================================================")
    print("   项目目录：" + str(ROOT))
    print("   时间：" + time.strftime("%Y-%m-%d %H:%M:%S"))

    # ---- 启动进度窗（Phase 11）----
    # 先把第一条进度写好再拉窗，窗口一出现就有内容可画；窗口是独立进程，
    # 它出任何问题都不影响下面的启动流程（最坏情况就是没有进度条）。
    global PROGRESS
    show_splash = splash_enabled(env)
    PROGRESS = splash_state.Progress(enabled=show_splash, logger=lambda line: print("  " + line))
    PROGRESS.start()
    if show_splash:
        spawn_splash()
        print("  启动进度显示在屏幕中央的进度条上（按 Esc 可关掉它，不影响启动）")

    milestone(10, "1/5 环境自检")
    if run_check_env() == 3 and not force:
        error("环境自检发现致命问题，已停止启动", "请按上面的建议处理后重新运行")
        print("")
        pause("  按回车键退出")
        return progress_failed()

    milestone(18, "2/5 检查 Ollama 本地模型")
    if port_open(ollama_host, ollama_port):
        print("  Ollama 已就绪（" + ollama_host + ":" + str(ollama_port) + "）")
    else:
        warn("Ollama 未运行", "若向量化使用本地模型（bge-m3）请先启动 Ollama；使用云端向量模型可忽略")

    milestone(26, "3/5 检查 Qdrant 向量数据库")
    rag_env_file = read_env(lightrag_home / ".env")
    effective_vector_storage = (
        os.environ.get("LIGHTRAG_VECTOR_STORAGE")
        or lightrag_env.get("LIGHTRAG_VECTOR_STORAGE")
        or rag_env_file.get("LIGHTRAG_VECTOR_STORAGE")
        or ""
    ).lower()
    if qdrant_enabled == "false":
        qdrant_needed = False
    elif qdrant_enabled == "auto" and "qdrant" not in effective_vector_storage:
        qdrant_needed = False
    else:
        qdrant_needed = True
    qdrant_ready = False
    if not qdrant_needed:
        print("  当前配置不需要 Qdrant（生效向量存储：" + (effective_vector_storage or "未指定") + "）")
        qdrant_ready = True
    elif port_open(qdrant_host, qdrant_port):
        print("  Qdrant 已就绪（" + qdrant_host + ":" + str(qdrant_port) + "）")
        qdrant_ready = True
    else:
        started_qdrant = False
        if qdrant_exe and Path(qdrant_exe).is_file():
            print("  正在启动本地 Qdrant...")
            spawn(qdrant_exe, [], Path(qdrant_exe).parent, "qdrant")
            save_marker("qdrant", "exe:" + str(Path(qdrant_exe).parent))
            started_qdrant = True
        else:
            try:
                listed = subprocess.run(
                    ["docker", "ps", "-a", "--filter", "name=qdrant", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=20, creationflags=no_window_flags())
                names = (listed.stdout or "").strip().splitlines()
                if names:
                    started = subprocess.run(["docker", "start", names[0]], capture_output=True, text=True,
                                             timeout=90, creationflags=no_window_flags())
                    if started.returncode == 0:
                        save_marker("qdrant", "docker:" + names[0])
                    started_qdrant = True
                elif listed.returncode == 0:
                    created = subprocess.run([
                        "docker", "run", "-d", "--name", "qdrant", "-p", "6333:6333",
                        "-v", "qdrant_storage:/qdrant/storage", "qdrant/qdrant"
                    ], capture_output=True, text=True, timeout=180, creationflags=no_window_flags())
                    if created.returncode == 0:
                        save_marker("qdrant", "docker:qdrant")
                    started_qdrant = True
            except (OSError, subprocess.SubprocessError):
                started_qdrant = False

        if started_qdrant:
            print("  等待 Qdrant 就绪（最多 90 秒）...")
            wait_started = time.time()
            deadline = wait_started + 90
            while time.time() < deadline:
                if port_open(qdrant_host, qdrant_port, 2.0):
                    break
                if PROGRESS is not None:
                    PROGRESS.creep(26, 34, time.time() - wait_started, "3/5 检查 Qdrant 向量数据库")
                time.sleep(3)

        if port_open(qdrant_host, qdrant_port):
            done("Qdrant（" + qdrant_host + ":" + str(qdrant_port) + "）")
            qdrant_ready = True
        else:
            warn("Qdrant 未启动（" + qdrant_host + ":" + str(qdrant_port) + "）",
                 "当前知识库使用 Qdrant 存储（Docker Desktop 未运行或容器未启动）")
    milestone(34, "4/5 启动 LightRAG 知识库服务")
    if lightrag_env:
        print("  来自设置页的配置将注入 LightRAG：")
        for env_name, env_value in user_settings.mask_env(lightrag_env).items():
            print("    " + env_name + " = " + env_value)
    lightrag_env_file = lightrag_home / ".env"
    if not lightrag_env_file.is_file():
        official_example = lightrag_home / "env.example"
        if official_example.is_file():
            shutil.copy2(official_example, lightrag_env_file)
            print("  [修复] LightRAG .env 缺失，已从官方 env.example 恢复")
        else:
            error("LightRAG 配置缺失", "请运行 setup.cmd 下载官方 env.example")
            FAILED = True
    if FAILED:
        pass
    elif port_open(lr_host, lr_port):
        skip("LightRAG（" + lr_host + ":" + str(lr_port) + "）")
    elif not lightrag_exe.is_file():
        error("找不到 lightrag-server", str(lightrag_exe))
        FAILED = True
    elif qdrant_needed and not qdrant_ready and not force:
        error("知识库需要 Qdrant，但 Qdrant 未运行，已取消启动 LightRAG（避免无提示崩溃）",
              "请先启动 Docker Desktop，等状态变为 Engine running 后再双击「启动.bat」")
        FAILED = True
    else:
        console_log = LOGS / "lightrag_console.txt"
        LOGS.mkdir(parents=True, exist_ok=True)
        handle = open(console_log, "w", encoding="utf-8", errors="replace")
        OPEN_HANDLES.append(handle)
        child_env = dict(os.environ)
        # LightRAG 启动横幅含 emoji；输出被重定向到文件时 Python 会退化为 GBK 编码，
        # 导致 UnicodeEncodeError 并在启动阶段崩溃，因此强制其子进程使用 UTF-8 输出。
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONUNBUFFERED"] = "1"
        # 注入网页设置页保存的模型/密钥等配置（环境变量优先于 LightRAG 自身 .env）
        child_env.update(lightrag_env)
        # 让 LightRAG 的监听端口与应用使用的 LIGHTRAG_URL 始终一致。
        child_env["HOST"] = (
            "127.0.0.1" if lr_host in ("127.0.0.1", "localhost", "0.0.0.0", "::", "")
            else lr_host
        )
        child_env["PORT"] = str(lr_port)
        process = subprocess.Popen(
            [str(lightrag_exe)], cwd=str(lightrag_home), env=child_env,
            stdout=handle, stderr=subprocess.STDOUT, creationflags=spawn_flags(),
        )
        save_pid("lightrag", process.pid)
        STARTED.append(("LightRAG", process.pid))
        print("  LightRAG 已启动（进程 " + str(process.pid) + "），正在等待就绪...")
        lr_health = "http://" + lr_host + ":" + str(lr_port) + "/health"
        if wait_http(lr_health, ready_timeout, "LightRAG", progress_range=(34, 64),
                     detail="4/5 启动 LightRAG 知识库服务"):
            done("LightRAG（" + lr_host + ":" + str(lr_port) + "）")
        else:
            error("LightRAG 未能在 " + str(ready_timeout) + " 秒内就绪（进程可能已退出）",
                  "详细日志：" + str(console_log))
            for label, log_path in (("LightRAG 控制台输出", console_log), ("LightRAG 自身日志", lightrag_home / "lightrag.log")):
                recent = tail_lines(log_path, 8)
                if recent:
                    print("  ---- " + label + " 末尾 ----")
                    for line in recent:
                        print("    " + line)
            DEGRADED.append(
                "LightRAG 未就绪；应用仍可使用普通聊天，知识库检索暂不可用"
            )

    milestone(70, "5/5 启动本应用（网页界面）")
    if port_open("127.0.0.1", app_port):
        if is_our_app(app_health):
            save_port(app_port)      # 已在运行：把它的端口记下来，停止/打开界面才对得上
            skip("本应用（" + app_root + "）")
        else:
            error("端口 " + str(app_port) + " 已被其他程序占用",
                  "请修改 .env 中的 APP_PORT，或先关闭占用该端口的程序")
            FAILED = True
    elif not python_exe.is_file():
        error("找不到 Python 运行环境", "缺少 " + str(python_exe) + "，请在项目目录执行：uv sync")
        FAILED = True
    else:
        app_args = ["main:app", "--host", app_host, "--port", str(app_port)]
        save_port(app_port)          # 记下本次真正用的端口（可能是环境变量指定的）
        if app_env:
            print("  来自设置页的配置将注入本应用：")
            for env_name, env_value in user_settings.mask_env(app_env).items():
                print("    " + env_name + " = " + env_value)
        process = spawn(str(uvicorn_exe), app_args, ROOT, "app", app_env)
        print("  本应用已启动（进程 " + str(process.pid) + "），正在等待就绪...")
        if wait_http(app_live, app_ready_timeout, "本应用", 3, progress_range=(70, 92),
                     detail="5/5 启动本应用（首次启动可能需要 1–3 分钟）",
                     process=process, accept=app_live_ok):
            done("本应用（" + app_root + "）")
        else:
            exit_code = process.poll()
            if exit_code is None:
                message = "本应用在 " + str(app_ready_timeout) + " 秒内未就绪"
            else:
                message = "本应用启动进程已退出（退出码 " + str(exit_code) + "）"
            error(message, "诊断文件：" + str(DIAGNOSTIC_FILE))
            write_startup_diagnostic(app_port, process)
            FAILED = True

    web_url = app_root + "/web"
    if FAILED:
        # 有失败就别让进度窗停在半路：变红 → 停 1.5 秒 → 关窗（摘要与提示照旧在后面打）
        progress_failed()
    else:
        # 服务真的就绪了，进度窗才写 100%：
        #   96%  → 仍是「正在启动中…」（收尾）
        #   100% → 换成「即将启动」，保持 1 秒让人看清，然后关窗、再打开浏览器
        if PROGRESS is not None and PROGRESS.enabled:
            PROGRESS.set(96, "服务已就绪，正在打开界面…")
            PROGRESS.done()
            time.sleep(1.0)
        if not skip_browser:
            print("")
            print("  正在打开浏览器：" + web_url)
            try:
                webbrowser.open(web_url)
            except Exception:
                warn("自动打开浏览器失败，请手动访问上面的地址")
            if PROGRESS is not None and PROGRESS.enabled:
                time.sleep(0.3)      # 让浏览器先出来，再让进度窗淡出，视觉上不打架
        close_splash()

    print("")
    print("  ------------------------------------------------------------")
    print("  启动结果")
    print("  ------------------------------------------------------------")
    print("    网页界面：" + web_url)
    # APP_HOST=0.0.0.0 时才提局域网：只在本机用的配置不该把人引到一个连不上的地址上
    if app_host in ("0.0.0.0", "::", ""):
        lan_ip = default_lan_ip()
        if lan_ip:
            print("    局域网访问：http://" + lan_ip + ":" + str(app_port) + "/web"
                  + "（平板 / 手机连同一个 WiFi）")
        else:
            print("    局域网访问：APP_HOST=0.0.0.0，但没取到局域网 IP；"
                  + "用 ipconfig 查看当前网卡 IPv4 后访问 http://IP:" + str(app_port) + "/web")
        print("    提示：电脑上的聊天页面关闭 25 秒后服务会自动停止；"
              + "想稍后用平板打开，请保持电脑页面开着，或在 .env 设置 CLOSE_STOPS_SERVICES=false")
    print("    健康检查：" + app_health)
    try:
        health = requests.get(app_health, timeout=20).json()
        print("    知识库服务：" + ("正常" if health.get("lightrag", {}).get("ok") else "未连接"))
        print("    向量数据库：" + ("正常" if health.get("qdrant", {}).get("ok") else "未连接"))
        print("    本地模型  ：" + ("正常" if health.get("ollama", {}).get("ok") else "未连接"))
        print("    总体状态  ：" + str(health.get("status")))
    except (requests.RequestException, ValueError):
        warn("暂时无法读取健康状态", "稍后可在浏览器打开 /health 查看")

    if STARTED:
        print("")
        print("  本次由本启动器拉起的服务：")
        for name, pid in STARTED:
            print("    " + name + "（进程 " + str(pid) + "）")

    print("")
    if SILENT:
        print("  停止服务：关闭聊天窗口 25 秒后会自动停止；也可双击桌面「停止本地知识库助手」")
        print("  启动日志：" + str(LOG_FILE))
    else:
        print("  停止服务：双击项目根目录的 stop.cmd")
    print("")

    if FAILED:
        print("  启动存在问题，请查看上面的失败提示。")
        if not no_pause:
            pause("  按回车键退出")
        return 3

    if DEGRADED:
        print("  启动完成，但有依赖未就绪：")
        for item in DEGRADED:
            print("    - " + item)
    else:
        print("  启动完成，可以开始使用。")
    if SILENT:
        print("  本次启动全程无窗口；服务日志见 logs/lightrag_console.txt 与 logs/app_console.txt")
    if not no_pause:
        pause("  按回车键关闭本窗口（服务在后台继续运行，日志在 logs 目录）")
    return 0


if __name__ == "__main__":
    CODE = 3
    try:
        CODE = main()
    finally:
        # 正常结束、失败、甚至抛异常，都不留下一个赖在屏幕上的进度窗
        close_splash(wait_seconds=0.8)
    if CODE != 0:
        # 静默模式没有窗口可看：失败时弹一个明确的提示框，成功不打扰
        detail_file = DIAGNOSTIC_FILE if DIAGNOSTIC_FILE.is_file() else LOG_FILE
        notify_error(
            "本地知识库助手 - 启动失败",
            "服务未能正常启动（退出码 " + str(CODE) + "）。\n"
            "请把下面的诊断文件发给维护者：\n" + str(detail_file),
        )
    sys.exit(CODE)
