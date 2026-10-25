"""统一日志（Phase 1 建立，Phase 3 接入请求中间件）。

Windows 兼容性处理：
1. 文件 handler 显式使用 UTF-8 编码，避免中文日志乱码；
2. 控制台在 CMD 默认代码页下可能乱码，这里在可用时把 stdout/stderr
   重新配置为 UTF-8（失败则静默忽略，不影响运行）。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

from app.core.paths import LOG_DIR

LOGGER_NAME = "app"
LOG_FILE_NAME = "app.log"
CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _ensure_utf8_console() -> None:
    """尽力把标准流切到 UTF-8，避免 Windows 控制台中文乱码。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def setup_logging(level: str = "INFO", *, force: bool = False) -> logging.Logger:
    """初始化并返回应用日志器（默认幂等，可重复调用）。"""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers and not force:
        return logger

    resolved_level = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(resolved_level)
    logger.propagate = False

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    _ensure_utf8_console()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(resolved_level)
    console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    logger.addHandler(console_handler)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler: Optional[logging.Handler] = TimedRotatingFileHandler(
            LOG_DIR / LOG_FILE_NAME,
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
        file_handler.setLevel(resolved_level)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("日志文件初始化失败，仅输出到控制台: %s", LOG_DIR / LOG_FILE_NAME)

    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """获取子日志器（形如 app.chat）。"""
    if name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME + "." + name)