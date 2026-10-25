"""静默运行的公共工具（Phase 9.2）。

给「耗时长、结束必须给用户回执」的维护脚本共用：

    scripts/migrate_data.py --silent          迁移数据进项目
    scripts/switch_to_local_vdb.py --silent   切换到本地文件向量库

（PowerShell 侧的 backup.ps1 -Silent 遵循同一套约定，见该文件内注释。）
日常启停另有 start.py --silent / stop.py --silent（Phase 9.1 已验收的自包含实现，
保留原样以免动到每天都要用的路径）。

三条约定（与 .clinerules 的「Windows 入口与静默约定」一致）：
1. 子进程一律带 CREATE_NO_WINDOW，绝不闪出命令行窗口；
2. 全程日志追加写入 logs\\<name>.log（静默时日志是唯一排查入口，逐行落盘，中途关掉也有痕迹）；
3. 结束时用 Windows 消息框给回执：失败 = 错误框（原因 + 日志路径），成功 = 信息框（关键结果 + 下一步）。
   日常启停「成功不打扰」，维护脚本「结束必回执」，因为用户看不到窗口里的进展。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.paths import LOG_DIR  # noqa: E402
LOGS = LOG_DIR

# 消息框图标（与 scripts/start.py、stop.py 用的同一套 user32 MessageBoxW 常量）
MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40


def no_window_flags() -> int:
    """Windows 下不创建控制台窗口（子进程不再闪出黑窗）。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_silent() -> bool:
    """是否静默模式（由 *_silent.vbs 传入 --silent）。"""
    return "--silent" in sys.argv


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


def notify(title: str, text: str, error: bool = False) -> None:
    """弹 Windows 消息框（静默模式下唯一「看得见」的出口）；非 Windows 或不可用时静默跳过。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, MB_ICONERROR if error else MB_ICONINFORMATION)
    except Exception:  # noqa: BLE001 - 弹框失败不能影响退出码
        pass


class SilentSession:
    """一次维护任务的「日志 + 回执」会话。

    用法：
        SESSION = silent_utils.SilentSession("migrate_data", "本地知识库助手 - 迁移数据")
        SESSION.start()
        ...
        return SESSION.finish(0, "迁移完成：……")      # 成功：写 [完成] 行并弹信息框
        return SESSION.finish(3, "", "复制失败：……")  # 失败：写 [失败] 行并弹错误框
    """

    def __init__(self, name: str, title: str) -> None:
        self.name = name                # 日志文件名（logs\<name>.log）
        self.title = title              # 消息框标题
        self.silent = is_silent()
        self.path = LOGS / (name + ".log")

    # ---------------- 日志 ----------------
    def start(self) -> None:
        """会话开始：写会话头（带时间，便于和上一次运行区分）。"""
        self.silent = is_silent()
        if self.silent:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = LOGS / (self.name + ".log")
        self.log("")
        self.log("===== " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + self.name + " =====")

    def log(self, text: str = "") -> None:
        """同时写控制台与日志文件（前台模式只写控制台）。"""
        print(text, flush=True)
        if not self.silent:
            return
        try:
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(str(text) + chr(10))
        except OSError:
            pass

    def attach(self, text: str) -> None:
        """把子进程输出逐行写进日志（加前缀便于区分）。"""
        for row in (text or "").splitlines():
            self.log("    | " + row)

    # ---------------- 子进程 ----------------
    def run_child(self, cmd, cwd=None, timeout=None) -> subprocess.CompletedProcess:
        """跑子进程：静默时把输出收进日志，前台时照旧直接显示在控制台。"""
        if not self.silent:
            return subprocess.run(cmd, cwd=cwd, timeout=timeout, creationflags=no_window_flags())
        done = subprocess.run(
            cmd,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            creationflags=no_window_flags(),
        )
        text = decode_output(done.stdout) + decode_output(done.stderr)
        if text.strip():
            self.attach(text)
        return done

    # ---------------- 回执 ----------------
    def finish(self, code: int, ok_text: str, fail_text: str = "") -> int:
        """收尾：写结论行；静默模式下弹一个结果框。返回退出码（命令行调用者仍看退出码）。"""
        self.log("")
        if code == 0:
            self.log("  [完成] " + ok_text)
            if self.silent:
                self.log("  详细日志：" + str(self.path))
                notify(self.title, ok_text + chr(10) + chr(10) + "详细日志：" + str(self.path), error=False)
            return 0
        reason = fail_text or ok_text
        self.log("  [失败] " + reason)
        if self.silent:
            self.log("  详细日志：" + str(self.path))
            notify(self.title, "未完成：" + reason + chr(10) + chr(10) + "详细日志：" + str(self.path), error=True)
        return code
