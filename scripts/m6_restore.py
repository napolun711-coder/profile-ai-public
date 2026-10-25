"""M6：从备份恢复记忆库/资料库，并重放当前墓碑。

必须先停服；脚本默认检查 127.0.0.1:<port>/health，服务仍在跑时拒绝。
用法：
    .venv\\Scripts\\python.exe scripts\\m6_restore.py --backup data\\backups\\backup-... --confirm
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
from app.services.m6_backup import DEFAULT_BACKUP_ROOT, DEFAULT_TOMBSTONES, restore_backup


def _service_running(port: int) -> bool:
    try:
        import requests

        response = requests.get("http://127.0.0.1:%d/health" % int(port), timeout=1.5)
        return response.status_code == 200
    except Exception:  # noqa: BLE001 - 连不上就当服务已停
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 备份恢复（需停服）")
    parser.add_argument("--backup", required=True, help="备份目录")
    parser.add_argument("--confirm", action="store_true", help="确认执行不可逆替换")
    parser.add_argument("--allow-running", action="store_true", help="跳过服务运行检查（不推荐）")
    parser.add_argument("--memory-db", default="")
    parser.add_argument("--tombstones", default=str(DEFAULT_TOMBSTONES))
    parser.add_argument("--root", default=str(DEFAULT_BACKUP_ROOT))
    args = parser.parse_args()

    if not args.confirm:
        print("拒绝执行：恢复会替换当前数据库，请加 --confirm。")
        return 2

    cfg = get_settings()
    if not args.allow_running and _service_running(int(cfg.app_port or 8100)):
        print("拒绝执行：服务仍在运行，请先停止服务（stop_silent.vbs / appctl stop）再恢复。")
        return 3

    memory_db = Path(args.memory_db) if args.memory_db else resolve_db_path(cfg.memory_db_path)
    result = restore_backup(
        Path(args.backup),
        memory_db=memory_db,
        tombstones=Path(args.tombstones),
        backup_root=Path(args.root),
        keep=int(cfg.memory_backup_keep or 14),
    )
    print("恢复完成：%s" % result["restored_from"])
    print("恢复前备份：%s" % result["pre_restore_backup"])
    print("墓碑重放：%s" % json.dumps(result["tombstones"], ensure_ascii=False))
    print("memory integrity：%s" % result["memory_integrity"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
