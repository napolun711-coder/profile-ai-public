"""P1：工具调用审计（JSONL、只记元数据）。

设计口径（与 P0 影子脚本一致）：
- 只记录“谁在什么时候调了哪个工具、结果如何、花了多久”；
- **不记录参数值、不记录查询原文、不记录正文**；参数只留键名与整体哈希；
- 写审计失败只告警，绝不影响工具调用本身；
- 按天一个文件：`logs/tools/YYYY-MM-DD.jsonl`。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.paths import LOG_DIR

logger = logging.getLogger(__name__)


class AuditLogger:
    """极简 JSONL 追加器；目录可注入（测试用临时目录，不碰真实 logs/）。"""

    def __init__(
        self,
        directory: Optional[Path] = None,
        *,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.directory = Path(directory) if directory is not None else LOG_DIR / "tools"
        self._logger = logger_ or logger
        self._lock = threading.Lock()

    def path_for(self, day: str = "") -> Path:
        """当天（或指定 `YYYY-MM-DD`）的审计文件路径。"""
        label = day or datetime.now().strftime("%Y-%m-%d")
        return self.directory / f"{label}.jsonl"

    def write(self, record: Dict[str, Any]) -> bool:
        """追加一条记录；成功 True，失败 False（失败不影响调用）。"""
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            self._logger.warning("工具审计记录无法序列化（已跳过）：%s", exc)
            return False

        path = self.path_for()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            return True
        except OSError as exc:
            self._logger.warning("写入工具审计失败（不影响调用）：%s：%s", path, exc)
            return False

    def read(self, day: str = "") -> List[Dict[str, Any]]:
        """读回某天的记录（诊断/测试用；损坏行跳过，不抛异常）。"""
        path = self.path_for(day)
        if not path.is_file():
            return []

        records: List[Dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return records

        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records


__all__ = ["AuditLogger"]
