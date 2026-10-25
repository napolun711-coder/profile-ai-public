"""启动进度状态（Phase 11）：启动器与进度窗之间唯一的通信方式。

为什么用「一个 JSON 文件」而不是管道 / socket / 环境变量：
  1. 启动器（scripts\\start.py）与进度窗（scripts\\splash.py）是两个独立进程，
     任何一个崩了都不能连累另一个 —— 读文件是「读到哪算哪」，没有握手、没有重连；
  2. 内容极小（< 200 字节），进度窗 60ms 读一次的开销可以忽略；
  3. 静默模式下排查只能靠 logs 目录，进度文件和日志放在一起，一眼就能看出「卡在哪一步」。

谁写谁读：
  写：scripts\\start.py  —— 每个里程碑写一次；等待类步骤（等 Ollama / LightRAG / 端口）每轮询一次
  读：scripts\\splash.py  —— 每 60ms 读一次；解析失败一律忽略，绝不因为显示层影响启动

JSON 字段：
  phase    running | done | closing | error
  percent  0-100（**只有真正就绪才写 100**，等待期靠渐近爬升，不会假到 100%）
  text     进度条上方那行大字（正在启动中… / 即将启动 / 启动失败…）
  detail   进度条下方小字（例如「4/5 启动 LightRAG 知识库服务」）
  ts       写入时的 unix 时间戳（进度窗据此判断「多久没更新了」）

写入用「临时文件 + os.replace」：os.replace 在 Windows 上是原子替换，
进度窗永远不会读到写了一半的 JSON。
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from app.core.paths import LOG_DIR  # noqa: E402
STATE_FILE = LOG_DIR / "splash.json"

PHASE_RUNNING = "running"
PHASE_DONE = "done"
PHASE_CLOSING = "closing"
PHASE_ERROR = "error"

TEXT_RUNNING = "正在启动中…"
TEXT_SOON = "即将启动"
TEXT_FAILED = "启动失败…"
TEXT_PREPARE = "正在准备…"


def state_path() -> Path:
    """进度文件路径（进度窗用 --state 覆盖时以参数为准）。"""
    return STATE_FILE


def clear() -> None:
    """删掉上一次启动残留的进度，避免新进度窗一开就显示上一次的结果。"""
    try:
        STATE_FILE.unlink()
    except OSError:
        pass


def write(phase: str, percent: float, text: str = "", detail: str = "") -> None:
    """原子写一次进度；任何异常都吞掉（进度显示永远不能影响启动）。"""
    payload = {
        "phase": str(phase),
        "percent": round(float(percent), 2),
        "text": str(text),
        "detail": str(detail),
        "ts": time.time(),
    }
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def read() -> dict[str, Any]:
    """读进度；文件不存在或内容不完整时返回空字典。"""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Progress:
    """启动器侧的进度上报器。

    三个约束：
      1. 同一百分比 + 同一文案不重复写盘（等待期每 0.5 秒 tick 一次，不做去重会白写很多次）；
      2. 任何异常都不能冒泡 —— 这是个「锦上添花」的功能，坏了也不能让启动失败；
      3. 顺手把进度也写一份到 start.log，静默模式下日志里能看到同样的时间线。
    """

    def __init__(self, enabled: bool = True, logger: Callable[[str], None] | None = None) -> None:
        self.enabled = bool(enabled)
        self.logger = logger
        self.percent = 0.0
        self.detail = ""
        self.phase = PHASE_RUNNING

    # ---------- 内部 ----------
    def _log(self, message: str) -> None:
        if self.logger is None:
            return
        try:
            self.logger(message)
        except Exception:
            pass

    def _emit(self, phase: str, percent: float, text: str, detail: str) -> None:
        if not self.enabled:
            return
        try:
            write(phase, percent, text, detail)
        except Exception:
            pass

    # ---------- 对外 ----------
    def start(self) -> None:
        """启动最开始的 3%：先清残留再写第一条。"""
        self.phase = PHASE_RUNNING
        self.percent = 3.0
        self.detail = TEXT_PREPARE
        if self.enabled:
            try:
                clear()
            except Exception:
                pass
        self._emit(PHASE_RUNNING, self.percent, TEXT_RUNNING, self.detail)

    def set(self, percent: float, detail: str = "", text: str = TEXT_RUNNING) -> None:
        """到达某个里程碑：百分比直接跳到位（跳变由进度窗缓动掉，不会一格一格抖）。"""
        try:
            percent = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            return
        if percent > self.percent:
            self.percent = percent
        if detail:
            self.detail = detail
        self.phase = PHASE_RUNNING
        self._emit(PHASE_RUNNING, self.percent, text, self.detail)
        self._log("进度 %d%% %s" % (round(self.percent), self.detail))

    def tick(self, detail: str = "") -> None:
        """等待类步骤每轮询一次：只更新时间，不推进百分比。"""
        self._emit(PHASE_RUNNING, self.percent, TEXT_RUNNING, detail or self.detail)

    def creep(self, start: float, end: float, elapsed: float, detail: str = "", tau: float = 7.0) -> None:
        """等待类步骤的「渐近爬升」：start → end，永不超过 end。

        用 1-e^(-t/tau) 是为了「一开始动得快、越等越慢」：
        用户在等 LightRAG 启动时能看到进度条在动（不是卡死），但拖得再久也不会假到 100%。
        """
        try:
            ratio = 1.0 - math.exp(-max(0.0, float(elapsed)) / max(0.5, float(tau)))
        except (TypeError, ValueError):
            return
        self.set(float(start) + (float(end) - float(start)) * ratio, detail)

    def done(self) -> None:
        """服务已就绪：唯一一次写 100%，并把文案换成「即将启动」。"""
        self.phase = PHASE_DONE
        self.percent = 100.0
        self._emit(PHASE_DONE, 100.0, TEXT_SOON, self.detail)
        self._log("进度 100% " + TEXT_SOON)

    def closing(self) -> None:
        """让进度窗自己淡出（比直接杀进程干净）。

        text 传空串是有意的：启动失败时窗口上是红字「启动失败…」，
        这里再传「即将启动」会把它顶掉，用户就看不到失败提示了。
        """
        self._emit(PHASE_CLOSING, self.percent, "", self.detail)

    def fail(self, detail: str = "") -> None:
        """启动失败：进度条变红，文案给出去哪看原因。"""
        self.phase = PHASE_ERROR
        self._emit(PHASE_ERROR, self.percent, TEXT_FAILED, detail or "详情见 logs\\start.log")
        self._log("进度 失败：" + (detail or ""))
