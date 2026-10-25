"""启动进度窗（Phase 11）：屏幕正中一块深色渐变进度条。

为什么必须是独立进程：进度窗是「看得见的等待」，它绝不能拖慢或中断启动。
所以它和启动器（scripts\\start.py）完全解耦 —— 启动器往 logs\\splash.json 写里程碑，
本进程每 60ms 读一次并平滑动画；本进程崩了 / 被 Esc 关掉 / 被杀掉，都不影响启动。

三条自保规则（避免留下关不掉的孤儿窗）：
  1. 每 0.5 秒确认「启动器进程还在」（--owner-pid），不在就自己退出；
  2. 最多显示 --max-seconds（默认 900 秒），到点自退；
  3. 命名互斥体保证同一时刻只有一个进度窗（连点两次启动不会叠出两个窗）。

显示规则（与启动器约定，见 splash_state.py）：
  phase=running → 大字「正在启动中…」，进度缓动到目标值（永不超过目标）
  phase=done    → 大字「即将启动」，进度补满 100%，等启动器发 phase=closing 后淡出
  phase=error   → 大字「启动失败…」（红），等启动器关闭
  状态文件超过 60 秒没更新 → 小字提示「启动时间较长…」，进度条继续缓慢爬升（看着不像卡死）

用法（一般由 start.py 自动拉起，不必手工执行）：
  python scripts\\splash.py --owner-pid 12345
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

try:
    import tkinter as tk
except Exception:  # pragma: no cover - 极少见：Python 未带 tkinter，此时启动照常，只是没有进度窗
    tk = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import splash_state  # noqa: E402  （同目录模块，必须在上面的 sys.path 处理之后导入）

ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.paths import LOG_DIR  # noqa: E402
LOGS = LOG_DIR
ICON_FILE = ROOT / "static" / "app.ico"

# ---- 设计尺寸（逻辑像素；实际会按系统 DPI 缩放，见 Splash.build）----
BASE_W, BASE_H = 420, 118
CARD_RADIUS = 14
ICON_X, ICON_Y, ICON_BOX = 22, 15, 22
TITLE_X, TITLE_Y = 54, 25
STATUS_Y = 47
BAR_X, BAR_Y, BAR_H = 22, 68, 8
DETAIL_Y = 90
TICK_MS = 60

BG = "#111827"            # 卡片底（与网页深色同色）
EDGE = "#1f2937"          # 卡片描边
TRACK = "#1f2937"         # 进度槽底色
TITLE_FG = "#cbd5e1"
STATUS_FG = "#f8fafc"
MUTED = "#94a3b8"
ERROR_FG = "#fca5a5"
GRAD_A = (59, 130, 246)   # #3b82f6（网页 logo 渐变起点）
GRAD_B = (139, 92, 246)   # #8b5cf6（网页 logo 渐变终点）
ERROR_A = (220, 38, 38)   # #dc2626
ERROR_B = (249, 115, 22)  # #f97316
TRANSPARENT_KEY = "#010203"   # 圆角外的透明色键（几乎不可能和卡片颜色撞）
MUTEX_NAME = "Local\\profile-ai-splash"
SHIMMER_COLS = 21
STALE_HINT = "启动时间较长，仍在等待…（详情见 logs\\start.log）"
TEXT_TITLE = "本地知识库助手"


# --------------------------------------------------------------------------- 小工具
def _append_log(path: Path | str, message: str) -> None:
    """进度窗自己的日志（logs\\splash.log）。日志写不进去也绝不报错。"""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fp:
            fp.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), message))
    except OSError:
        pass


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in rgb)


def _gradient_rgb(t: float, start=GRAD_A, end=GRAD_B):
    t = max(0.0, min(1.0, float(t)))
    return tuple(round(start[i] + (end[i] - start[i]) * t) for i in range(3))


def _lighten(rgb, amount: float):
    return tuple(round(c + (255 - c) * amount) for c in rgb)


def _rounded_points(x: float, y: float, w: float, h: float, r: float, steps: int = 7):
    """圆角矩形顶点序列（Tk 的 create_polygon 用；圆角自己算，比 smooth 样条规整）。"""
    r = max(1.0, min(r, w / 2.0, h / 2.0))
    points: list[float] = []
    corners = (
        (x + r, y + r, 180.0, 270.0),          # 左上
        (x + w - r, y + r, 270.0, 360.0),      # 右上
        (x + w - r, y + h - r, 0.0, 90.0),     # 右下
        (x + r, y + h - r, 90.0, 180.0),       # 左下
    )
    for cx, cy, start, end in corners:
        for step in range(steps + 1):
            angle = math.radians(start + (end - start) * step / steps)
            points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def _load_icon(size: int):
    """从 static\\app.ico 里取最接近 size 的 PNG 条目，转成 Tk 图片；失败返回 None。

    Tk 8.6 自带 PNG 解码，所以 base64 之后直接喂给 PhotoImage 就行，不需要 Pillow。
    """
    if tk is None:
        return None
    try:
        raw = ICON_FILE.read_bytes()
        if raw[:4] != b"\x00\x00\x01\x00":
            return None
        count = struct.unpack_from("<H", raw, 4)[0]
        best = None
        for index in range(count):
            entry = 6 + 16 * index
            dim = raw[entry] or 256
            length, offset = struct.unpack_from("<II", raw, entry + 8)
            payload = raw[offset:offset + length]
            if payload[:8] != b"\x89PNG\r\n\x1a\n":
                continue
            if best is None or abs(dim - size) < abs(best[0] - size):
                best = (dim, payload)
        if best is None:
            return None
        return tk.PhotoImage(data=base64.b64encode(best[1]).decode("ascii"))
    except Exception:
        return None


def _set_dpi_awareness() -> None:
    """优先 per-monitor v2：不声明的话，高分屏上窗口会被系统位图拉伸，字发虚。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _acquire_singleton():
    """命名互斥体；已有实例在显示时返回 None（最多等 1.2 秒，让上一个自己退干净）。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return True          # 拿不到 API 就不拦：宁可多显示一个窗，也不要卡住启动
    for _ in range(4):
        handle = kernel32.CreateMutexW(None, False, ctypes.c_wchar_p(MUTEX_NAME))
        if not handle:
            return True
        if ctypes.get_last_error() != 183:   # ERROR_ALREADY_EXISTS
            return handle
        kernel32.CloseHandle(handle)
        time.sleep(0.3)
    return None


def _owner_alive(pid: int) -> bool:
    """启动器进程是否还活着。探测失败一律按「活着」处理，避免误关。"""
    if pid <= 0:
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                              # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def _write_own_pid(path: Path) -> None:
    """把自己的真实 PID 写下来。

    启动器拿到的 Popen.pid 未必等于窗口进程：uv 生成的 .venv\\Scripts\\python.exe 是个
    「转发壳」，真正的解释器是它的子进程。启动器收尾时按这个文件兜底关闭，
    因此「文件还在 = 进度窗还活着」，本进程正常退出时会自己删掉它。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def _clear_own_pid(path: Path) -> None:
    """退出时删掉自己的 PID 文件（只删内容确实是自己的那份，避免误删别人的）。"""
    try:
        if path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


class Splash:
    """进度窗本体：只负责「把 logs\\splash.json 画出来」，不做任何启动判断。"""

    def __init__(self, args) -> None:
        self.path = Path(args.state)
        self.owner = int(args.owner_pid)
        self.log_path = Path(args.log)
        self.min_seconds = float(args.min_seconds)
        self.max_seconds = float(args.max_seconds)
        self.started = time.time()
        self.deadline = self.started + self.max_seconds

        self.target = 2.0            # 启动器给的目标百分比
        self.displayed = 0.0         # 真正画出来的百分比（缓动跟随 target）
        self.phase = splash_state.PHASE_RUNNING
        self.status_text = splash_state.TEXT_RUNNING
        self.detail = ""
        self.updated_at = time.time()
        self.state_mtime = 0.0
        self.filled = -1             # 已填色的列数（-1 = 一列都没填）
        self.last_owner_check = 0.0
        self.last_log = 0.0
        self.closed = False
        self.scale = 1.0
        self.root = None
        self.canvas = None
        self.cols: list = []

    # ------------------------------------------------------------------ 构建界面
    def _s(self, value: float) -> int:
        """设计尺寸 → 实际像素（按系统 DPI 缩放）。"""
        return max(1, int(round(value * self.scale)))

    def build(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = max(1.0, min(2.5, float(self.root.winfo_fpixels("1i")) / 96.0))
        self.w = self._s(BASE_W)
        self.h = self._s(BASE_H)

        self.root.title(TEXT_TITLE)
        self.root.overrideredirect(True)          # 无边框、不进任务栏
        for attr, value in (("-topmost", True), ("-toolwindow", True), ("-alpha", 0.98)):
            try:
                self.root.attributes(attr, value)
            except tk.TclError:
                pass

        self.transparent = True
        try:
            self.root.configure(bg=TRANSPARENT_KEY)
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            self.transparent = False              # 拿不到透明色键就退化成直角卡片
            self.root.configure(bg=BG)

        canvas_bg = TRANSPARENT_KEY if self.transparent else BG
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h, bg=canvas_bg,
                                highlightthickness=0, bd=0, takefocus=0)
        self.canvas.pack()

        x = max(0, (self.root.winfo_screenwidth() - self.w) // 2)
        y = max(0, (self.root.winfo_screenheight() - self.h) // 2)
        self.root.geometry("%dx%d+%d+%d" % (self.w, self.h, x, y))

        self._draw_static()
        self.root.deiconify()
        try:
            self.root.lift()
        except tk.TclError:
            pass
        self.root.bind("<Escape>", lambda _event: self.close())
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _draw_static(self) -> None:
        """画一次就不再变的那些东西：卡片、图标、标题、进度槽、光带、小字。"""
        small = ("Microsoft YaHei UI", -self._s(11))
        self.font_title = ("Microsoft YaHei UI", -self._s(13))
        self.font_status = ("Microsoft YaHei UI", -self._s(15), "bold")
        self.font_detail = small

        # 卡片（圆角靠透明色键抠出来）
        self.canvas.create_polygon(
            _rounded_points(0, 0, self.w, self.h, self._s(CARD_RADIUS)),
            fill=BG, outline=EDGE, width=max(1, self._s(1)),
        )

        # 应用图标（直接复用 static\app.ico，与桌面快捷方式同一个图标）
        self.icon_photo = _load_icon(self._s(ICON_BOX))
        if self.icon_photo is not None:
            self.canvas.create_image(self._s(ICON_X), self._s(ICON_Y), image=self.icon_photo, anchor="nw")
        else:
            self._draw_logo_fallback()

        self.canvas.create_text(self._s(TITLE_X), self._s(TITLE_Y), text=TEXT_TITLE,
                                anchor="w", fill=TITLE_FG, font=self.font_title)
        self.status_item = self.canvas.create_text(self.w // 2, self._s(STATUS_Y), text=self.status_text,
                                                   anchor="center", fill=STATUS_FG, font=self.font_status)

        # 进度槽：按 3px（设计尺寸）一列预建好，之后只改颜色，不重建
        self.bar_x = self._s(BAR_X)
        self.bar_w = self.w - 2 * self.bar_x
        self.bar_y = self._s(BAR_Y)
        self.bar_height = self._s(BAR_H)
        self.step = max(2, self._s(3))
        x = self.bar_x
        while x < self.bar_x + self.bar_w:
            width = min(self.step, self.bar_x + self.bar_w - x)
            item = self.canvas.create_rectangle(x, self.bar_y, x + width, self.bar_y + self.bar_height,
                                                width=0, outline="", fill=TRACK)
            self.cols.append((x, item))
            x += width

        # 光带：叠在渐变之上，每帧只改这 21 根，开销可以忽略
        self.shimmer = [
            self.canvas.create_rectangle(self.bar_x, self.bar_y, self.bar_x, self.bar_y + self.bar_height,
                                         width=0, outline="", fill=TRACK, state="hidden")
            for _ in range(SHIMMER_COLS)
        ]
        self.percent_item = self.canvas.create_text(self.bar_x + self.bar_w, self._s(DETAIL_Y),
                                                    text="0%", anchor="e", fill=MUTED, font=small)
        self.detail_item = self.canvas.create_text(self.bar_x, self._s(DETAIL_Y),
                                                   text="", anchor="w", fill=MUTED, font=small)

    def _draw_logo_fallback(self) -> None:
        """图标文件缺失时的兜底：一个渐变圆角小方块 + 白纸，不至于空一块。"""
        box = self._s(ICON_BOX)
        left, top = self._s(ICON_X), self._s(ICON_Y)
        self.canvas.create_polygon(_rounded_points(left, top, box, box, box * 0.28),
                                   fill=_hex(GRAD_A), outline="")
        self.canvas.create_polygon(
            _rounded_points(left + box * 0.28, top + box * 0.18, box * 0.44, box * 0.62, box * 0.12),
            fill="#ffffff", outline="")

    # ------------------------------------------------------------------ 状态 / 绘制
    def _read_state(self) -> None:
        """读一次 logs\\splash.json（按修改时间做门槛，文件没变就不解析）。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self.state_mtime:
            return
        self.state_mtime = mtime
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                      # 文件正好被替换/写坏：这一帧当没发生，下一帧再说
        if not isinstance(data, dict):
            return
        self.updated_at = time.time()
        phase = str(data.get("phase") or splash_state.PHASE_RUNNING)
        if phase == splash_state.PHASE_ERROR and self.phase != splash_state.PHASE_ERROR:
            self.filled = -1            # 让进度条整体重画成红色
        self.phase = phase
        try:
            percent = float(data.get("percent") or 0.0)
        except (TypeError, ValueError):
            percent = 0.0
        self.target = max(self.target, min(100.0, percent))   # 只前进，不后退（避免抖动）
        text = str(data.get("text") or "")
        if text:
            self.status_text = text
        self.detail = str(data.get("detail") or "")
        if self.phase == splash_state.PHASE_ERROR and self.detail == "":
            self.detail = "详情见 logs\\start.log"
        self._apply_text()

    def _apply_text(self) -> None:
        color = ERROR_FG if self.phase == splash_state.PHASE_ERROR else STATUS_FG
        self.canvas.itemconfigure(self.status_item, text=self.status_text, fill=color)
        self.canvas.itemconfigure(self.detail_item, text=self.detail)

    def _base_rgb(self, index: int):
        span = max(1.0, float(len(self.cols) - 1))
        t = index / span
        if self.phase == splash_state.PHASE_ERROR:
            return _gradient_rgb(t, ERROR_A, ERROR_B)
        return _gradient_rgb(t)

    def _column_color(self, index: int, filled: int) -> str:
        if index > filled:
            return TRACK
        base = self._base_rgb(index)
        if index == filled:
            return _hex(_lighten(base, 0.42))     # 前沿提亮，看着像「正在推进」
        return _hex(base)

    def _paint(self) -> None:
        fill_w = self.bar_w * (self.displayed / 100.0)
        idx = max(-1, min(len(self.cols) - 1, int(fill_w // self.step)))
        if idx != self.filled:
            low = min(idx, self.filled) + 1
            high = max(idx, self.filled)
            for i in range(max(0, low), min(len(self.cols), high + 1)):
                self.canvas.itemconfigure(self.cols[i][1], fill=self._column_color(i, idx))
            if 0 <= self.filled < len(self.cols) and self.filled != idx:
                self.canvas.itemconfigure(self.cols[self.filled][1], fill=self._column_color(self.filled, idx))
            self.filled = idx
        self.canvas.itemconfigure(self.percent_item, text="%d%%" % round(self.displayed))
        self._paint_shimmer(idx)

    def _paint_shimmer(self, filled: int) -> None:
        """一条缓缓扫过的光带，只覆盖已填充的部分：让「等待」看起来是活的。"""
        if self.phase != splash_state.PHASE_RUNNING or filled <= 3:
            for item in self.shimmer:
                self.canvas.itemconfigure(item, state="hidden")
            return
        pos = ((time.time() / 1.6) % 1.0) * 1.08 - 0.04
        center = max(0.0, min(1.0, pos)) * float(filled)
        half = max(1.0, len(self.shimmer) / 2.0)
        for k, item in enumerate(self.shimmer):
            index = int(round(center - half + k))
            if index < 0 or index > filled:
                self.canvas.itemconfigure(item, state="hidden")
                continue
            distance = abs(index - center) / half
            strength = max(0.0, 1.0 - distance * distance) * 0.30
            if strength <= 0.01:
                self.canvas.itemconfigure(item, state="hidden")
                continue
            x = self.bar_x + index * self.step
            self.canvas.coords(item, x, self.bar_y, x + self.step, self.bar_y + self.bar_height)
            self.canvas.itemconfigure(item, fill=_hex(_lighten(self._base_rgb(index), strength)), state="normal")

    # ------------------------------------------------------------------ 主循环
    def log(self, message: str) -> None:
        _append_log(self.log_path, message)

    def _tick(self) -> None:
        if self.closed:
            return
        now = time.time()
        self._read_state()

        # 1) 启动器让关就关（但至少显示 min_seconds，避免快到看不见）
        if self.phase == splash_state.PHASE_CLOSING and now - self.started >= self.min_seconds:
            self.log("收到关闭指令，进度窗退出（显示 %.0f%%）" % self.displayed)
            self.close()
            return
        # 2) 最长显示时间
        if now >= self.deadline:
            self.log("超过最长显示时间 %.0f 秒，自动关闭" % self.max_seconds)
            self.close()
            return
        # 3) 启动器没了（被强杀 / 崩了）就别赖在屏幕上
        if now - self.last_owner_check > 0.5:
            self.last_owner_check = now
            if not _owner_alive(self.owner):
                self.log("启动器进程 %d 已退出，进度窗自动关闭" % self.owner)
                self.close()
                return

        # 4) 长时间没有新进度：给个提示，但进度条继续慢慢爬（不是卡死）
        if self.phase == splash_state.PHASE_RUNNING and now - self.updated_at > 60.0 and self.detail != STALE_HINT:
            self.detail = STALE_HINT
            self._apply_text()

        # 5) 缓动：显示值只追不放，永不超过目标；目标停住时允许极慢爬升
        if self.displayed < self.target:
            self.displayed = min(self.target, self.displayed + max(0.3, (self.target - self.displayed) * 0.18))
        elif self.phase == splash_state.PHASE_RUNNING and now - self.updated_at > 1.2:
            ceiling = min(self.target + 4.0, 96.0)      # 96%：绝不在没就绪时假装到 100%
            if self.displayed < ceiling:
                self.displayed = min(ceiling, self.displayed + 0.07)

        self._paint()
        if now - self.last_log > 2.0:
            self.last_log = now
            self.log("显示 %.0f%% / 目标 %.0f%% / %s / %s" % (self.displayed, self.target, self.phase, self.detail or "-"))
        self.root.after(TICK_MS, self._tick)

    def close(self) -> None:
        """淡出后销毁。用户可以按 Esc 只关窗口，不影响后面的启动。"""
        if self.closed:
            return
        self.closed = True
        try:
            for alpha in (0.8, 0.6, 0.4, 0.22, 0.08):
                self.root.attributes("-alpha", alpha)
                self.root.update()
                time.sleep(0.035)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        self.log("进度窗启动：owner-pid=%d 尺寸=%dx%d 状态文件=%s" % (self.owner, self.w, self.h, self.path))
        self._paint()
        self.root.after(TICK_MS, self._tick)
        self.root.mainloop()
        self.log("进度窗已关闭")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="本地知识库助手 - 启动进度窗（由 scripts\\start.py 拉起）")
    parser.add_argument("--owner-pid", type=int, default=0, help="启动器进程号；它退出后进度窗自动关闭")
    parser.add_argument("--state", default=str(splash_state.state_path()), help="进度状态文件（logs\\splash.json）")
    parser.add_argument("--log", default=str(LOGS / "splash.log"), help="进度窗自己的日志")
    parser.add_argument("--min-seconds", type=float, default=1.2, help="最少显示时长，秒")
    parser.add_argument("--max-seconds", type=float, default=900.0, help="最长显示时长，秒")
    args = parser.parse_args(argv)

    if tk is None:
        _append_log(args.log, "当前 Python 没有 tkinter，跳过进度窗（不影响启动）")
        return 0

    _set_dpi_awareness()
    if _acquire_singleton() is None:
        _append_log(args.log, "已有进度窗在显示，本次跳过")
        return 0

    pid_file = LOGS / "splash.pid"
    _write_own_pid(pid_file)
    splash = None
    try:
        splash = Splash(args)
        splash.build()
        splash.run()
    except Exception as exc:                      # 进度窗的任何问题都不许影响启动
        _append_log(args.log, "进度窗异常退出（不影响启动）：%r" % (exc,))
        if splash is not None and splash.root is not None:
            try:
                splash.root.destroy()
            except Exception:
                pass
    finally:
        _clear_own_pid(pid_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())



