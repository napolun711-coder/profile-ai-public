"""本地知识库助手 - 停止服务（Phase 4.1；Phase 10 起统一走 lifecycle）。

停止由启动器拉起的服务：本应用、LightRAG 知识库，以及「本次由启动器拉起的」Qdrant。
真正的实现只有一份：scripts/lifecycle.py（含端口反查兜底与「只停自己拉起的服务」规则），
本文件只负责命令行体验：把结论打印出来、没停干净时给提示。

用法（推荐静默）：双击项目根目录的 stop_silent.vbs（全程无命令行窗口）；
用法（前台查看）：双击 stop.bat；或用统一入口 appctl.bat stop。

参数：
  --silent   静默模式：只在「没停干净（进程还在 / 端口仍被占用）」时弹提示框

只想看看「现在谁在跑」：python scripts\\lifecycle.py status（只读，不会停任何东西）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))      # 支持从任意目录调用

import lifecycle  # noqa: E402 - 注入 scripts 目录后导入（该目录不是包）

# 静默模式（由 stop_silent.vbs 传入 --silent）
SILENT = "--silent" in sys.argv


def notify_problem(title: str, text: str) -> None:
    """静默模式：只有没停干净时才弹提示框，正常停止不打扰。"""
    if not SILENT:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x30)
    except Exception:  # noqa: BLE001 - 非 Windows 或消息框不可用时不阻断退出
        pass


def main() -> int:
    env = lifecycle.read_env()
    app_port = lifecycle.app_port(env)
    lr_port = lifecycle.lightrag_port(env)

    print("")
    print("  本地知识库助手 - 停止服务")
    print("")

    messages, problems = lifecycle.stop_all(reason="stop.py")
    for text in messages:
        print("  " + text)

    app_alive = lifecycle.port_open("127.0.0.1", app_port)
    lr_alive = lifecycle.port_open("127.0.0.1", lr_port)
    print("")
    print("  端口状态：")
    print("    本应用端口 " + str(app_port) + "：" + ("仍在监听" if app_alive else "已释放"))
    print("    知识库端口 " + str(lr_port) + "：" + ("仍在监听" if lr_alive else "已释放"))
    print("")
    print("  提示：完整记录在 logs\\stop.log；下次启动双击桌面「本地知识库助手」即可。")
    print("")

    if SILENT and problems:
        notify_problem("本地知识库助手 - 停止未完成", "\n".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
