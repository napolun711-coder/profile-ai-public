"""路径常量（Phase 1：消除对当前工作目录 CWD 的依赖）。

所有路径都基于本文件位置推导为绝对路径，因此无论从哪个目录启动
（uvicorn main:app / python main.py / uv run ...），静态资源与日志目录都能正确定位。
"""

from pathlib import Path

CORE_DIR: Path = Path(__file__).resolve().parent          # app/core
APP_DIR: Path = Path(__file__).resolve().parents[1]        # app
BASE_DIR: Path = Path(__file__).resolve().parents[2]       # 项目根目录

# Phase 6 起前端目录独立到项目根 static/（对外 URL /static 与 /web 保持不变）
STATIC_DIR: Path = BASE_DIR / "static"

LOG_DIR: Path = BASE_DIR / "logs"
ENV_FILE: Path = BASE_DIR / ".env"

# ---------- Phase 8：文档解析与质量门的落盘目录 ----------
DATA_DIR: Path = BASE_DIR / "data"
# M1：对话/记忆数据库（SQLite）。放在 data/ 下，Docker 的 data 卷已覆盖。
MEMORY_DIR: Path = DATA_DIR / "memory"
UPLOAD_DIR: Path = DATA_DIR / "uploads"      # 上传原件留档（documents.py 使用）
PARSED_DIR: Path = DATA_DIR / "parsed"       # 每个文件一个子目录：content.md + result.json
KNOWLEDGE_DIR: Path = BASE_DIR / "knowledge"  # 通过质量门的统一 Markdown 留档
OUTPUT_DIR: Path = BASE_DIR / "output"        # 产出目录（导出/报告预留）

# ---------- Phase 16：文件夹轮询自动入库 + 原件留档 + 可视化文档管理 ----------
# 全部落在 data\ 下：原生模式下是项目里的 data\，容器模式下是挂载进来的
# 运行时数据始终位于独立数据目录，不随源码仓库发布。
INBOX_DIR: Path = DATA_DIR / "inbox"                 # 丢进这里的文件会被自动入库
INBOX_TAKEN_DIR: Path = INBOX_DIR / "_taken"         # 已处理过（含重复）→ 归档，不再扫描
INBOX_FAILED_DIR: Path = INBOX_DIR / "_failed"       # 受理失败 → 隔离（附同名 .reason.txt）
ORIGINALS_DIR: Path = DATA_DIR / "originals"         # 原件留档（按 YYYY-MM 分月）
WATCH_DIR: Path = DATA_DIR / "watch"                 # 轮询状态（不参与业务）
WATCH_INDEX: Path = WATCH_DIR / "index.json"         # sha256 → 已受理过的文件（防重复入库）

FALLBACK_FILENAME = "upload.bin"


def safe_filename(name: str, fallback: str = FALLBACK_FILENAME) -> str:
    """把任意文件名收敛为安全的单段文件名（与上传留档的规则一致）。

    只保留字母/数字与 . _ -，去掉首尾的点与空格；为空时用 fallback。
    """
    base = Path(name or fallback).name
    kept = [char if (char.isalnum() or char in "._-") else "_" for char in base]
    cleaned = "".join(kept).strip(" .")
    return cleaned or fallback

