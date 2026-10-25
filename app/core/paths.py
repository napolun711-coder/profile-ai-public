"""应用路径与用户数据目录。

源码目录只保存程序和模板；用户的配置、知识库、数据库、日志、模型与备份默认放在
平台用户数据目录，不写入 Git 工作树。

可用 PROFILE_AI_HOME 覆盖默认位置：
- Windows: %LOCALAPPDATA%\\ProfileAI
- macOS:   ~/Library/Application Support/ProfileAI
- Linux:   $XDG_DATA_HOME/profile-ai 或 ~/.local/share/profile-ai
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

CORE_DIR: Path = Path(__file__).resolve().parent
APP_DIR: Path = Path(__file__).resolve().parents[1]
BASE_DIR: Path = Path(__file__).resolve().parents[2]
STATIC_DIR: Path = BASE_DIR / "static"
ENV_FILE: Path = BASE_DIR / ".env"
BUNDLED_CONFIG_DIR: Path = BASE_DIR / "config"
EXAMPLE_SETTINGS_FILE: Path = BUNDLED_CONFIG_DIR / "user-settings.example.json"


def _default_profile_home() -> Path:
    override = str(os.environ.get("PROFILE_AI_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA") or "").strip()
        return Path(base) / "ProfileAI" if base else Path.home() / "AppData" / "Local" / "ProfileAI"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ProfileAI"
    base = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    return Path(base) / "profile-ai" if base else Path.home() / ".local" / "share" / "profile-ai"


PROFILE_AI_HOME: Path = _default_profile_home()
CONFIG_DIR: Path = PROFILE_AI_HOME / "config"
SETTINGS_FILE: Path = CONFIG_DIR / "user-settings.json"
RUNTIME_FILE: Path = CONFIG_DIR / "runtime.json"
LOG_DIR: Path = PROFILE_AI_HOME / "logs"
DATA_DIR: Path = PROFILE_AI_HOME / "data"
MEMORY_DIR: Path = DATA_DIR / "memory"
UPLOAD_DIR: Path = DATA_DIR / "uploads"
PARSED_DIR: Path = DATA_DIR / "parsed"
KNOWLEDGE_DIR: Path = PROFILE_AI_HOME / "knowledge"
OUTPUT_DIR: Path = PROFILE_AI_HOME / "output"
MODELS_DIR: Path = PROFILE_AI_HOME / "models"
RAG_STORAGE_DIR: Path = PROFILE_AI_HOME / "rag_storage"
BACKEND_DIR: Path = PROFILE_AI_HOME / "backend"
LIGHTRAG_HOME: Path = BACKEND_DIR / "lightrag"
LIGHTRAG_INPUT_DIR: Path = DATA_DIR / "lightrag_input"
BACKUP_DIR: Path = PROFILE_AI_HOME / "backups"

INBOX_DIR: Path = DATA_DIR / "inbox"
INBOX_TAKEN_DIR: Path = INBOX_DIR / "_taken"
INBOX_FAILED_DIR: Path = INBOX_DIR / "_failed"
ORIGINALS_DIR: Path = DATA_DIR / "originals"
WATCH_DIR: Path = DATA_DIR / "watch"
WATCH_INDEX: Path = WATCH_DIR / "index.json"

FALLBACK_FILENAME = "upload.bin"


def ensure_directories() -> List[Path]:
    """Create the profile-owned runtime directories and return them."""
    directories = [
        PROFILE_AI_HOME,
        CONFIG_DIR,
        LOG_DIR,
        DATA_DIR,
        MEMORY_DIR,
        UPLOAD_DIR,
        PARSED_DIR,
        KNOWLEDGE_DIR,
        OUTPUT_DIR,
        MODELS_DIR,
        RAG_STORAGE_DIR,
        BACKEND_DIR,
        LIGHTRAG_HOME,
        LIGHTRAG_INPUT_DIR,
        BACKUP_DIR,
        INBOX_DIR,
        INBOX_TAKEN_DIR,
        INBOX_FAILED_DIR,
        ORIGINALS_DIR,
        WATCH_DIR,
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    return directories


def safe_filename(name: str, fallback: str = FALLBACK_FILENAME) -> str:
    """Reduce an arbitrary file name to one safe path segment."""
    base = Path(name or fallback).name
    kept = [char if (char.isalnum() or char in "._-") else "_" for char in base]
    cleaned = "".join(kept).strip(" .")
    return cleaned or fallback
