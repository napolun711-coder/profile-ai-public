"""统一入口（Phase 10）：一条命令覆盖日常全部操作。

为什么要有它：以前根目录有 start / stop / restart / backup / migrate_data / switch_vdb
六对入口（每对还有各自的 _silent.vbs），名字记不住、也容易点错。现在全部收敛到本文件，
桌面快捷方式、开始菜单、命令行都指向 appctl.vbs / appctl.bat。旧的 12 个入口保留为
「转发壳」，双击效果与以前一致。

用法（三选一，效果相同）：
    双击 appctl.vbs                       默认 = 启动（无窗口，只有失败才弹框）
    appctl.bat status                     可见窗口（排障时看输出）
    .venv\\Scripts\\python.exe scripts\\appctl.py restart

命令：
    start       启动全部服务（默认命令）
    stop        停止本应用拉起的全部服务（含 LightRAG；共享的 Qdrant 不动）
    restart     先停后起
    status      只读查看端口 / 进程 / 启动记录，不做任何修改
    open        打开网页界面（服务没起时会提示先启动）
    backup      备份数据（scripts\\backup.ps1）
    migrate     把知识库数据搬进项目（scripts\\migrate_data.py）
    switch-vdb  切换到本地文件向量库（scripts\\switch_to_local_vdb.py）
    shortcut    创建 / 修复桌面与开始菜单快捷方式

参数：
    --silent    静默：写日志，结束只弹结果框（appctl.vbs 默认加上）
    --no-pause  结束不等回车（vbs 用）
    其余参数原样转发给对应脚本，例如：start --force --ready-timeout 120
                                              start --no-splash（启动时不显示屏幕中央的进度条）
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

sys.path.insert(0, str(SCRIPTS))

import lifecycle  # noqa: E402 - 注入 scripts 目录后导入（该目录不是包）

COMMANDS = ("start", "stop", "restart", "status", "open", "backup", "migrate", "switch-vdb", "shortcut")

SILENT = "--silent" in sys.argv
NO_PAUSE = "--no-pause" in sys.argv

HELP = """本地知识库助手 - 统一入口（appctl）

  appctl.vbs <命令>          无窗口执行（推荐；不写命令 = start）
  appctl.bat <命令>          可见窗口执行（排障用）

命令：
  start       启动全部服务（默认）
  stop        停止本应用拉起的全部服务
  restart     先停后起
  status      只读查看运行状态
  open        打开网页界面
  backup      备份数据
  migrate     把知识库数据搬进项目
  switch-vdb  切换到本地文件向量库
  shortcut    创建 / 修复桌面快捷方式

示例：
  appctl.vbs                  启动（双击 appctl.vbs 等价）
  appctl.bat status           看看现在谁在跑
  appctl.vbs restart          重启
"""


def no_window_flags() -> int:
    """Windows 下不创建控制台窗口。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def python_exe() -> str:
    """优先用项目自带的 Python（保证依赖一致），不存在时用当前解释器。"""
    return str(PYTHON) if PYTHON.is_file() else sys.executable


def forward_flags(command: str) -> list:
    """把 appctl 的参数翻译成子脚本认识的参数（各脚本的约定不同）。"""
    if command == "backup":
        return ["-Silent"] if SILENT else []
    if command in ("start", "stop", "restart", "migrate", "switch-vdb"):
        flags = ["--silent"] if SILENT else []
        if command in ("start", "restart") and NO_PAUSE:
            flags.append("--no-pause")
        return flags
    return []


def extra_args(args) -> list:
    """去掉 appctl 自己的参数，剩下的原样转发（例如 --force）。"""
    return [item for item in args if item not in ("--silent", "--no-pause")]


def run_child(cmd) -> int:
    """运行子脚本并返回退出码（静默模式下子脚本自己写日志、自己弹结果框）。"""
    try:
        done = subprocess.run(cmd, cwd=str(ROOT), creationflags=no_window_flags())
    except OSError as exc:
        print("  [失败] 无法运行：" + " ".join(cmd) + "（" + exc.__class__.__name__ + "）")
        return 4
    return done.returncode


def python_script(script_file: str, command: str, extras) -> int:
    """运行 scripts\\<script_file>（用 venv 的 Python，参数按命令翻译）。"""
    return run_child([python_exe(), str(SCRIPTS / script_file)] + forward_flags(command) + list(extras))


def powershell_script(name: str, params) -> int:
    """运行 scripts\\<name>.ps1（-ExecutionPolicy Bypass，避免受系统策略影响）。"""
    return run_child(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / name)]
        + list(params)
    )


def open_web() -> int:
    """打开网页界面；服务没起时给明确指引（不自动启动，避免双击两次起两份）。"""
    # 用「实际在听的端口」：端口被系统环境变量改过、或 .env 与运行值不一致时也能打开
    # （见 scripts/lifecycle.py 的 app_port_candidates 说明）
    candidates = lifecycle.app_port_candidates()
    port = lifecycle.running_app_port()
    if not lifecycle.port_open("127.0.0.1", port):
        print(
            "  服务还没启动（端口 "
            + "、".join(str(item) for item in candidates)
            + " 都未监听）。"
        )
        print("  请先启动：双击桌面「本地知识库助手」，或双击项目根目录的 appctl.vbs")
        return 1
    url = "http://127.0.0.1:" + str(port) + "/web"
    print("  正在打开：" + url)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打不开浏览器不算失败
        print("  自动打开失败，请手动访问上面的地址")
    return 0


def show_status() -> int:
    """只读状态：端口在听谁、是不是本应用拉起的、启动记录还剩什么。"""
    for line in lifecycle.format_status():
        print(line)
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] in ("-h", "--help", "/?", "help"):
        print(HELP)
        return 0

    command = "start"
    if args and not args[0].startswith("-"):
        command = args.pop(0).lower()
    if command not in COMMANDS:
        print("  未知命令：" + command)
        print(HELP)
        return 2
    if not PYTHON.is_file():
        print("  找不到 Python 环境：" + str(PYTHON))
        print("  请先双击项目根目录的 install_env.bat 完成环境安装。")
        return 3

    extras = extra_args(args)
    print("")
    print("  本地知识库助手 - " + command)
    print("")

    if command == "status":
        code = show_status()
    elif command == "open":
        code = open_web()
    elif command == "stop":
        code = python_script("stop.py", "stop", extras)
    elif command == "start":
        code = python_script("start.py", "start", extras)
    elif command == "restart":
        # 先停后起：stop.py 只停本应用拉起的服务，因此不会碰到共享的 Qdrant
        python_script("stop.py", "stop", extras)
        code = python_script("start.py", "start", extras)
    elif command == "backup":
        code = powershell_script("backup.ps1", forward_flags("backup"))
    elif command == "migrate":
        code = python_script("migrate_data.py", "migrate", extras)
    elif command == "switch-vdb":
        code = python_script("switch_to_local_vdb.py", "switch-vdb", extras)
    else:  # shortcut
        code = powershell_script("install_shortcuts.ps1", extras)

    print("")
    return code


if __name__ == "__main__":
    sys.exit(main())
