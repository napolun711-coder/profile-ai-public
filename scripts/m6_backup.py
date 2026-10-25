"""M6：创建在线备份（SQLite backup API + manifest + 轮换）。

用法：
    .venv\\Scripts\\python.exe scripts\\m6_backup.py
    .venv\\Scripts\\python.exe scripts\\m6_backup.py --keep 14
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import get_settings
from app.memory.db import resolve_db_path
from app.services.m6_backup import DEFAULT_BACKUP_ROOT, DEFAULT_TOMBSTONES, backup_all


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 SQLite 在线备份")
    parser.add_argument("--keep", type=int, default=0)
    parser.add_argument("--root", default=str(DEFAULT_BACKUP_ROOT))
    parser.add_argument("--memory-db", default="")
    parser.add_argument("--tombstones", default=str(DEFAULT_TOMBSTONES))
    args = parser.parse_args()

    cfg = get_settings()
    memory_db = Path(args.memory_db) if args.memory_db else resolve_db_path(cfg.memory_db_path)
    keep = args.keep or int(cfg.memory_backup_keep or 14)
    result = backup_all(
        memory_db=memory_db,
        tombstones=Path(args.tombstones),
        backup_root=Path(args.root),
        keep=keep,
    )
    manifest = result["manifest"]
    print("备份完成：%s" % result["directory"])
    print("保留份数：%d" % keep)
    print(json.dumps(manifest.get("counts") or {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
