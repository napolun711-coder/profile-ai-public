"""服务生命周期（Phase 10）：把「启停」收敛到唯一实现。

为什么要单独一层：启停入口现在有四个——命令行、双击 vbs/bat、桌面快捷方式、
网页里的「停止服务」。若各写一套，迟早出现「停不干净」。Phase 10 之前就有实测
缺陷：logs\\stop.log 里三个服务都写「未找到进程记录」，而 9621 仍在监听
（pid 文件与实际进程对不上）。因此停止逻辑只在本模块实现一次，其它入口都调用它。

停止范围（只停「本应用拉起的」服务）：
  1. 本应用（uvicorn，logs\\app.pid）
  2. LightRAG 知识库（logs\\lightrag.pid；没有 pid 记录时用端口 9621 反查兜底）
  3. 本地 Qdrant：仅当本次是启动器拉起的（logs\\qdrant.pid 或 logs\\qdrant.started）
     别人先启好的共享容器（例如与 Dify 共用）一律不动。
  Ollama 是用户级服务，从不停止。

安全约束（沿用 Phase 4.1，避免误杀）：
  - 只有进程名在 ALLOWED_NAMES 内才允许 taskkill；
  - 端口兜底反查到的进程还要核对命令行（须指向本项目 / LightRAG 目录），
    否则只报告不动作——本机名为 python.exe 的进程太多了；
  - 所有子进程带 CREATE_NO_WINDOW，绝不闪黑窗。

被谁调用：
  scripts\\stop.py            命令行 / stop_silent.vbs
  scripts\\appctl.py          统一入口（start / stop / restart / status）
  app\\routers\\control.py     网页「关闭窗口即停服」（defer_app=True 形式）
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"

# 允许停止的进程名（与 Phase 4.1 一致；不在名单内一律不动）
ALLOWED_NAMES = ("uvicorn.exe", "python.exe", "lightrag-server.exe", "qdrant.exe")

APP_NAME = "app"
LIGHTRAG_NAME = "lightrag"
QDRANT_NAME = "qdrant"
OLLAMA_NAME = "ollama"

APP_PID_FILE = LOGS / "app.pid"
LIGHTRAG_PID_FILE = LOGS / "lightrag.pid"
QDRANT_PID_FILE = LOGS / "qdrant.pid"
QDRANT_MARKER = LOGS / "qdrant.started"     # 内容形如 docker:qdrant 或 exe:<路径>
APP_PORT_FILE = LOGS / "app.port"           # 启动器实际用的端口（端口漂移时停止/打开界面对得上）

STOP_LOG = LOGS / "stop.log"
STOP_LOG_MAX_BYTES = 512 * 1024

DEFAULT_APP_PORT = 8100
DEFAULT_LIGHTRAG_PORT = 9621
DEFAULT_QDRANT_PORT = 6333
DEFAULT_OLLAMA_PORT = 11434

SERVICE_LABELS = {
    APP_NAME: "本应用",
    LIGHTRAG_NAME: "知识库（LightRAG）",
    QDRANT_NAME: "向量库（Qdrant）",
    OLLAMA_NAME: "本地模型（Ollama）",
}


# ============================================================
# 基础工具
# ============================================================

def no_window_flags() -> int:
    """Windows 下不创建控制台窗口（tasklist/taskkill 不再闪出黑窗）。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def decode_output(data) -> str:
    """子进程输出解码：先按 utf-8，失败再按中文 Windows 的本地代码页（GBK）。"""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, AttributeError):
            continue
    return data.decode("utf-8", errors="replace")


def now_text() -> str:
    """日志时间戳（与 logs 里其它文件保持同样的可读格式）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_env(path: Optional[Path] = None) -> Dict[str, str]:
    """读取 .env（简单解析，规则与 app/core/config.py 保持一致）。"""
    target = path or (ROOT / ".env")
    data: Dict[str, str] = {}
    if not target.is_file():
        return data
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError:
        return data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        data[key.strip()] = value.strip()
    return data


def env_int(env: Dict[str, str], key: str, default: int) -> int:
    """取整数配置项；写错时回落默认值（不打断停止流程）。"""
    try:
        return int(str(env.get(key, "")).strip())
    except (TypeError, ValueError):
        return default


def port_from_url(url: str, default: int) -> int:
    """从 URL / host:port 文本里取端口（配置写成什么形式都能用）。"""
    text = (url or "").strip()
    if not text:
        return default
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):            # IPv6：[::1]:9621
        text = text.rsplit("]", 1)[-1]
    if ":" in text:
        tail = text.rsplit(":", 1)[1].strip()
        if tail.isdigit():
            return int(tail)
    return default


def app_port(env: Optional[Dict[str, str]] = None) -> int:
    """本应用端口：APP_PORT 优先，兼容 PORT（与 app/core/config.py 的别名一致）。

    读取顺序必须与启动器 scripts\\start.py 完全一致：**系统环境变量 > .env > 默认值**
    （见 .clinerules 的配置读取顺序）。这条实测踩过坑：环境变量里写了 APP_PORT=8137，
    启动器按规则把服务起在 8137，而这里只读 .env 的 8100 —— 于是 stop.log 写「本应用：
    未在运行」，服务却还在跑（停不掉、界面也打不开）。env 显式传入时只按传入值解析，
    保持纯函数语义（便于单测）。
    """
    if env is not None:
        values: Dict[str, str] = dict(env)
    else:
        values = dict(read_env())
        for key in ("APP_PORT", "PORT"):
            env_value = os.environ.get(key, "")
            if str(env_value).strip():
                values[key] = str(env_value).strip()
    if str(values.get("APP_PORT", "")).strip():
        return env_int(values, "APP_PORT", DEFAULT_APP_PORT)
    return env_int(values, "PORT", DEFAULT_APP_PORT)


def read_port_record(path: Optional[Path] = None) -> int:
    """读取启动器记下的「实际用的端口」（logs\\app.port）；没有 / 内容非法时返回 0。"""
    target = path if path is not None else APP_PORT_FILE
    if not target.is_file():
        return 0
    try:
        text = target.read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return 0
    if not text.isdigit():
        return 0
    port = int(text)
    return port if 0 < port < 65536 else 0


def forget_app_port() -> None:
    """清掉「实际用的端口」记录（停止之后就不再有效）。"""
    remove_file(APP_PORT_FILE)


def app_port_candidates(env: Optional[Dict[str, str]] = None) -> List[int]:
    """停止 / 打开界面时要尝试的端口：**实际端口在前，配置端口在后**（去重、忽略 0）。

    为什么要两个：端口并不总和 .env 一样 —— 实测遇到过环境变量 APP_PORT=8137 起了服务、
    而 .env 写着 8100（还有「配置端口被别的程序占用」的情况）。只认一个就会出现
    「服务在跑，却报未在运行 / 界面打不开」。多试一个端口的代价只是几次端口探测。
    """
    ports: List[int] = []
    for port in (read_port_record(), app_port(env)):
        if port and port not in ports:
            ports.append(port)
    return ports


def running_app_port(env: Optional[Dict[str, str]] = None) -> int:
    """当前真正在监听的本应用端口（status / 打开界面用）。都没在监听时返回配置端口。"""
    candidates = app_port_candidates(env)
    for port in candidates:
        if port_open("127.0.0.1", port):
            return port
    return candidates[-1] if candidates else DEFAULT_APP_PORT


def lightrag_port(env: Optional[Dict[str, str]] = None) -> int:
    """知识库端口：从 LIGHTRAG_URL 里取。"""
    values = env if env is not None else read_env()
    return port_from_url(values.get("LIGHTRAG_URL", ""), DEFAULT_LIGHTRAG_PORT)


def qdrant_port(env: Optional[Dict[str, str]] = None) -> int:
    """向量库端口：从 QDRANT_URL 里取。"""
    values = env if env is not None else read_env()
    return port_from_url(values.get("QDRANT_URL", ""), DEFAULT_QDRANT_PORT)


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口是否有人监听（用连接探测，不依赖 netstat 的输出格式）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port_closed(host: str, port: int, timeout: float = 8.0) -> bool:
    """等端口释放（TCP 挥手要一点时间，刚 taskkill 完立刻探测可能还在）。"""
    deadline = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < deadline:
        if not port_open(host, port, 1.0):
            return True
    return not port_open(host, port, 1.0)


def read_pid(path: Optional[Path]) -> int:
    """读取进程记录；不存在 / 内容非法时返回 0。"""
    if path is None or not path.is_file():
        return 0
    text = path.read_text(encoding="ascii", errors="ignore").strip()
    return int(text) if text.isdigit() else 0


def remove_file(path: Optional[Path]) -> None:
    """删除文件（失败不报错：清理记录不能打断停止流程）。"""
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def read_marker(name: str) -> str:
    """读取「这个服务是启动器拉起的」记录（docker 容器没有 pid，用标记文件）。"""
    path = LOGS / (name + ".started")
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def write_marker(name: str, value: str) -> None:
    """写入启动记录（由 scripts/start.py 在成功拉起服务后调用）。"""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / (name + ".started")).write_text(value, encoding="utf-8")
    except OSError:
        pass


# ============================================================
# 进程与端口查询
# ============================================================

def query_process_image(pid: int) -> str:
    """查询进程的可执行文件名（用于避免误杀复用 PID 的其他程序）。"""
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "PID eq " + str(pid), "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    body = decode_output(result.stdout)
    for line in body.strip().splitlines():
        entry = line.strip()
        if not entry:
            continue
        if not entry.startswith('"'):
            return ""
        image = entry.split(",")[0].strip().strip('"')
        return image if image.lower().endswith(".exe") else ""
    return ""


def _cim_query(pids: Sequence[int], fields: str) -> List[dict]:
    """一次 PowerShell 调用批量查询进程信息；失败返回空列表（调用方据此保守处理）。"""
    ids = [str(int(pid)) for pid in pids if int(pid) > 0]
    if not ids:
        return []
    where = " or ".join("ProcessId=" + item for item in ids)
    command = (
        "Get-CimInstance Win32_Process -Filter '" + where + "' | "
        "Select-Object " + fields + " | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, timeout=30, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    body = decode_output(result.stdout).strip()
    if not body:
        return []
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    items = payload if isinstance(payload, list) else [payload]
    return [item for item in items if isinstance(item, dict)]


def process_command_lines(pids: Sequence[int]) -> Dict[int, str]:
    """批量查询命令行：{pid: 命令行}；查询不到时该 pid 不在结果里。"""
    data: Dict[int, str] = {}
    for item in _cim_query(pids, "ProcessId,CommandLine"):
        try:
            data[int(item.get("ProcessId"))] = str(item.get("CommandLine") or "")
        except (TypeError, ValueError):
            continue
    return data


def process_parents(pids: Sequence[int]) -> Dict[int, int]:
    """批量查询父进程：{pid: 父 pid}。"""
    data: Dict[int, int] = {}
    for item in _cim_query(pids, "ProcessId,ParentProcessId"):
        try:
            data[int(item.get("ProcessId"))] = int(item.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            continue
    return data


def iter_listening_pids(netstat_text: str, port: int) -> List[int]:
    """从 netstat -ano 输出里取出监听指定端口的进程号（纯函数，便于单测）。"""
    pids: List[int] = []
    for raw in (netstat_text or "").splitlines():
        parts = raw.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(":" + str(port)):
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if parts[4].isdigit():
            pid = int(parts[4])
            if pid not in pids:
                pids.append(pid)
    return pids


def pids_on_port(port: int) -> List[int]:
    """谁在监听这个端口（IPv4 / IPv6 都算）。"""
    if not port:
        return []
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, timeout=20, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return iter_listening_pids(decode_output(result.stdout), port)


def command_belongs_to_project(cmdline: str, hints: Sequence[str]) -> bool:
    """命令行是否指向本项目（纯函数）。hints 为空时不做判断（返回 True）。"""
    if not hints:
        return True
    text = str(cmdline or "").lower().replace("/", "\\")
    for hint in hints:
        token = str(hint or "").strip().lower().replace("/", "\\")
        if token and token in text:
            return True
    return False


def topmost_allowed_ancestor(pid: int, cmdlines: Dict[int, str], hints: Sequence[str], limit: int = 6) -> int:
    """从 pid 往上找「仍属于本项目进程链」的最顶层进程。

    为什么需要：LightRAG 实际是三层进程（python 包装 → python → lightrag-server），
    监听端口的往往是里层进程；只 taskkill /T 杀掉它，会把外面的包装进程留下
    （Phase 10 之前 9621 反复「停不掉」就是这个原因）。从监听者往上走，只要父进程
    也在允许名单内、命令行同样指向本项目，就继续往上，最后一并结束整棵树。
    """
    current = pid
    for _ in range(max(1, limit)):
        parents = process_parents([current])
        parent = parents.get(current, 0)
        if parent <= 0 or parent == current:
            break
        image = query_process_image(parent).lower()
        if image not in ALLOWED_NAMES:
            break
        parent_cmd = cmdlines.get(parent)
        if parent_cmd is None:
            parent_cmd = process_command_lines([parent]).get(parent)
        if parent_cmd is None or not command_belongs_to_project(parent_cmd, hints):
            break
        current = parent
    return current


def kill_pid(pid: int) -> bool:
    """结束进程树（/T 连子进程一起；/F 强制）。"""
    if pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=30, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ============================================================
# 停止：单个服务
# ============================================================

def stop_service(
    name: str,
    *,
    pid_file: Optional[Path] = None,
    port: Optional[int] = None,
    hints: Sequence[str] = (),
    allow_port_fallback: bool = True,
    label: str = "",
) -> str:
    """停止一个服务，返回一句人话结论。

    判定顺序：
      a) 端口上没有监听 → 顺手清理过期记录，返回「未在运行」；
      b) 端口上的进程就是我们记录的 pid → 直接结束（记录可信）；
      c) pid 记录对不上 / 不存在 → 端口反查 + 进程名白名单 + 命令行核对 + 向上找进程树根，
         确认是本项目的进程才结束，否则只报告不动作（本机同名 python.exe 太多）。
    """
    title = label or SERVICE_LABELS.get(name, name)
    recorded = read_pid(pid_file)
    live = pids_on_port(port) if port else []

    if port and not live:
        if recorded:
            remove_file(pid_file)
            return title + "：未在运行（已清理过期记录）"
        return title + "：未在运行"

    if live and not recorded and not allow_port_fallback:
        return title + "：端口 " + str(port) + " 被其他程序占用，不是本应用拉起的，已跳过"

    if recorded and (not live or recorded in live):
        targets = [recorded]
    elif live:
        targets = list(live)
    elif recorded:
        targets = [recorded]
    else:
        return title + "：未找到进程记录（或不是由本启动器拉起的）"

    fallback = [pid for pid in targets if pid != recorded]
    cmdlines = process_command_lines(fallback) if fallback else {}

    stopped: List[int] = []
    refused: List[str] = []
    failed: List[int] = []
    for pid in targets:
        image = query_process_image(pid).lower()
        if image and image not in ALLOWED_NAMES:
            refused.append(str(pid) + "（" + image + "，不在允许名单内）")
            continue
        target = pid
        if pid != recorded:
            cmdline = cmdlines.get(pid)
            if cmdline is None:
                refused.append(str(pid) + "（无法确认进程归属）")
                continue
            if not command_belongs_to_project(cmdline, hints):
                refused.append(str(pid) + "（命令行与本项目不符：" + image + "）")
                continue
            target = topmost_allowed_ancestor(pid, cmdlines, hints)
        if kill_pid(target):
            if target not in stopped:
                stopped.append(target)
        elif target == pid:
            failed.append(pid)

    parts: List[str] = []
    if stopped:
        remove_file(pid_file)
        parts.append(title + "：已停止（进程 " + "、".join(str(item) for item in stopped) + "）")
    if refused:
        parts.append(title + "：已跳过 " + "、".join(refused) + "（避免误杀）")
    if failed:
        parts.append(title + "：停止未成功（进程 " + "、".join(str(item) for item in failed) + "），请手动关闭对应窗口")
    if not parts:
        return title + "：无变化"
    return " ".join(parts)


def lightrag_hints(env: Optional[Dict[str, str]] = None) -> List[str]:
    """判断「哪个 python.exe 才是知识库」用的线索（命令行里出现任意一条即算）。"""
    values = env if env is not None else read_env()
    candidates = [
        str(ROOT / "lightrag"),                 # 项目内自带的 LightRAG
        values.get("LIGHTRAG_HOME", "").strip(),  # .env 里配置的安装目录
        "lightrag/",                          # 历史默认位置
        "lightrag-server",
        "lightrag",                              # 兜底：命令行里带 lightrag 的才动
    ]
    hints: List[str] = []
    for item in candidates:
        if item and item not in hints:
            hints.append(item)
    return hints


def stop_lightrag(port: Optional[int] = None, env: Optional[Dict[str, str]] = None) -> str:
    """停止知识库服务（有 pid 记录走 pid；没有就用端口 9621 反查兜底）。"""
    values = env if env is not None else read_env()
    return stop_service(
        LIGHTRAG_NAME,
        pid_file=LIGHTRAG_PID_FILE,
        port=port or lightrag_port(values),
        hints=lightrag_hints(values),
    )


def stop_qdrant(env: Optional[Dict[str, str]] = None) -> str:
    """只停「本应用拉起的」Qdrant。

    docker 容器由启动器 `docker start / docker run` 拉起时会留下 logs\\qdrant.started；
    本地 qdrant.exe 由启动器 spawn 时会留下 logs\\qdrant.pid。
    两者都没有 → 说明这个 Qdrant 是别人（例如 Dify / Docker Desktop）启的，一律不动。
    """
    values = env if env is not None else read_env()
    title = SERVICE_LABELS[QDRANT_NAME]
    marker = read_marker(QDRANT_NAME)
    if marker.startswith("docker:"):
        container = marker.split(":", 1)[1].strip() or QDRANT_NAME
        try:
            done = subprocess.run(
                ["docker", "stop", container],
                capture_output=True, timeout=120, creationflags=no_window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return title + "：停止容器失败（" + exc.__class__.__name__ + "）"
        if done.returncode == 0:
            remove_file(QDRANT_MARKER)
            return title + "：已停止容器 " + container + "（本次由启动器拉起）"
        detail = decode_output(done.stderr).strip().replace("\r", " ").replace("\n", " ")
        return title + "：停止容器未成功（" + (detail[:120] or "docker stop 返回非 0") + "）"
    if read_pid(QDRANT_PID_FILE):
        hint = marker.split(":", 1)[1].strip() if marker.startswith("exe:") else ""
        return stop_service(
            QDRANT_NAME,
            pid_file=QDRANT_PID_FILE,
            port=qdrant_port(values),
            hints=[hint] if hint else (),
            allow_port_fallback=False,
        )
    return title + "：不是本应用拉起的，保持运行（共享向量库不动）"


# ============================================================
# 停止：全部 / 状态查询 / 日志
# ============================================================

def stop_all(
    reason: str = "cli",
    *,
    defer_app: bool = False,
    include_qdrant: bool = True,
) -> Tuple[List[str], List[str]]:
    """停止本应用拉起的全部服务，返回（给人看的结论、仍未停干净的项）。

    defer_app=True 供网页路径使用：当前请求所在的进程就是本应用，不能在这里 taskkill
    自己（/T 会把同一棵进程树一起带走），所以只停同级服务，最后一行说明「本应用将随本次
    请求退出」，由调用方在响应返回后结束进程（见 app/routers/control.py）。
    """
    env = read_env()
    a_ports = app_port_candidates(env)
    lr_port = lightrag_port(env)
    messages: List[str] = []

    if defer_app:
        recorded = read_pid(APP_PID_FILE)
        remove_file(APP_PID_FILE)
        forget_app_port()
        detail = "进程 " + str(recorded) if recorded else "进程号未知"
        messages.append(SERVICE_LABELS[APP_NAME] + "：将随本次请求退出（" + detail + "）")
    else:
        # 实际端口在前、配置端口在后（app_port_candidates）：本应用只会占一个端口，停到就收工，
        # 都「未在运行」时把最后那句原样报给用户（含「已清理过期记录」）。
        last = ""
        for candidate in a_ports:
            last = stop_service(
                APP_NAME, pid_file=APP_PID_FILE, port=candidate, hints=[str(ROOT), ROOT.name]
            )
            if "未在运行" not in last:
                break
        messages.append(last)
        forget_app_port()

    messages.append(stop_lightrag(lr_port, env))
    if include_qdrant:
        messages.append(stop_qdrant(env))

    problems: List[str] = []
    for text in messages:
        if "停止未成功" in text or "停止容器未成功" in text or "停止容器失败" in text:
            problems.append(text)
    if not defer_app:
        for candidate in a_ports:
            if not wait_port_closed("127.0.0.1", candidate, 4.0):
                problems.append("本应用端口 " + str(candidate) + " 仍在监听")
    if not wait_port_closed("127.0.0.1", lr_port, 8.0):
        problems.append("知识库端口 " + str(lr_port) + " 仍在监听")

    write_stop_log(reason, messages, problems)
    return messages, problems


def write_stop_log(reason: str, messages: Sequence[str], problems: Sequence[str]) -> None:
    """把本次停止写进 logs\\stop.log（静默时没有窗口可看，这是唯一线索）。"""
    lines = [now_text() + "  [停止] 原因：" + reason]
    lines.extend("  " + item for item in messages)
    if problems:
        lines.append("  未停干净：")
        lines.extend("    - " + item for item in problems)
    else:
        lines.append("  结果：全部已停止")
    lines.append("")
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        if STOP_LOG.is_file() and STOP_LOG.stat().st_size > STOP_LOG_MAX_BYTES:
            STOP_LOG.unlink()
        with STOP_LOG.open("a", encoding="utf-8", errors="replace") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def docker_container_state(name: str = QDRANT_NAME) -> str:
    """查询 docker 容器状态（status 展示用；docker 不可用时返回空串）。"""
    try:
        done = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=" + name, "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, timeout=25, creationflags=no_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return decode_output(done.stdout).strip().replace("\r", "").replace("\n", "；")


def owned_by_app(name: str, pids: Sequence[int], cmdlines: Dict[int, str], env: Dict[str, str]) -> bool:
    """这些进程是不是本应用拉起的（用于 status 展示，也决定停哪些）。"""
    if name == APP_NAME:
        recorded = read_pid(APP_PID_FILE)
        if recorded and recorded in pids:
            return True
        return any(
            command_belongs_to_project(cmdlines.get(pid, ""), [str(ROOT), ROOT.name]) for pid in pids
        )
    if name == LIGHTRAG_NAME:
        recorded = read_pid(LIGHTRAG_PID_FILE)
        if recorded and recorded in pids:
            return True
        return any(command_belongs_to_project(cmdlines.get(pid, ""), lightrag_hints(env)) for pid in pids)
    if name == QDRANT_NAME:
        return bool(read_marker(QDRANT_NAME) or read_pid(QDRANT_PID_FILE))
    return False


def status() -> Dict[str, object]:
    """只读检查：每个端口在听谁、是不是本应用拉起的（不做任何修改）。"""
    env = read_env()
    ports = (
        # 本应用用「真正在听的那个端口」：配置端口被占用 / 环境变量改过端口时，
        # 只显示 .env 的值会让人以为服务没起来（实测过）。
        (APP_NAME, running_app_port(env)),
        (LIGHTRAG_NAME, lightrag_port(env)),
        (QDRANT_NAME, qdrant_port(env)),
        (OLLAMA_NAME, DEFAULT_OLLAMA_PORT),
    )
    services: List[Dict[str, object]] = []
    problems: List[str] = []
    for name, port in ports:
        pids = pids_on_port(port)
        cmdlines = process_command_lines(pids)
        services.append({
            "name": name,
            "label": SERVICE_LABELS.get(name, name),
            "port": port,
            "pids": pids,
            "images": [query_process_image(pid) or "?" for pid in pids],
            "ours": owned_by_app(name, pids, cmdlines, env),
        })
    files = {
        "app.pid": read_pid(APP_PID_FILE),
        "app.port": read_port_record(),
        "lightrag.pid": read_pid(LIGHTRAG_PID_FILE),
        "qdrant.pid": read_pid(QDRANT_PID_FILE),
        "qdrant.started": read_marker(QDRANT_NAME),
    }
    if not services[0]["pids"]:
        problems.append("本应用未运行（双击桌面「本地知识库助手」即可启动）")
    return {
        "services": services,
        "files": files,
        "qdrant_container": docker_container_state(),
        "problems": problems,
    }


def format_status(data: Optional[Dict[str, object]] = None) -> List[str]:
    """把 status() 的结果排成人看的几行（命令行与 appctl status 共用）。"""
    payload = data if data is not None else status()
    lines: List[str] = []
    for item in payload.get("services") or []:
        pids = list(item.get("pids") or [])
        images = list(item.get("images") or [])
        if pids:
            detail = "、".join(
                str(pid) + "（" + str(images[index] if index < len(images) else "?") + "）"
                for index, pid in enumerate(pids)
            )
            state = "监听中：" + detail
        else:
            state = "未运行"
        owner = "本应用拉起" if item.get("ours") else ("外部进程" if pids else "—")
        lines.append(
            "  " + str(item.get("label")) + "（端口 " + str(item.get("port")) + "）：" + state
            + "   [" + owner + "]"
        )
    container = str(payload.get("qdrant_container") or "")
    if container:
        lines.append("  Docker 容器：" + container)
    files = payload.get("files") or {}
    lines.append(
        "  启动记录：" + "、".join(
            key + "=" + (str(value) if value else "无") for key, value in files.items()
        )
    )
    for item in payload.get("problems") or []:
        lines.append("  ! " + str(item))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    """调试用入口：python scripts\\lifecycle.py [status|stop]（日常请用 appctl.vbs）。"""
    args = list(argv if argv is not None else [])
    action = args[0].lower() if args else "status"
    print("")
    if action == "stop":
        messages, problems = stop_all(reason="lifecycle-cli")
        for text in messages:
            print("  " + text)
        for text in problems:
            print("  ! " + text)
        print("")
        return 1 if problems else 0
    print("  本地知识库助手 - 运行状态")
    print("")
    for line in format_status():
        print(line)
    print("")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))





