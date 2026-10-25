"""M6：用真实备份做一次“恢复演练”，但只恢复到临时目录，不碰线上库。

用法：
    .venv\\Scripts\\python.exe scripts/m6_restore_drill.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.m6_backup import (
    DEFAULT_BACKUP_ROOT,
    MEMORY_NAME,
    MANIFEST_NAME,
    restore_backup,
    verify_backup,
)


def latest_backup(root: Path) -> Path:
    dirs = sorted(
        [path for path in root.iterdir() if (path / MANIFEST_NAME).is_file()],
        key=lambda path: path.name,
    )
    if not dirs:
        raise SystemExit("没有可用备份：%s" % root)
    return dirs[-1]


def main() -> int:
    backup = latest_backup(DEFAULT_BACKUP_ROOT)
    manifest = verify_backup(backup)
    with tempfile.TemporaryDirectory(prefix="profile-ai-restore-drill-") as td:
        work = Path(td)
        memory_db = work / "memory.db"
        tombstones = work / "deletions.jsonl"
        if (backup / "deletions.jsonl").is_file():
            shutil.copy2(backup / "deletions.jsonl", tombstones)
        result = restore_backup(
            backup,
            memory_db=memory_db,
            tombstones=tombstones,
            backup_root=work / "backups",
            keep=3,
        )
        counts = {}
        if memory_db.is_file():
            from app.memory.db import MemoryDatabase
            from app.memory.store import MemoryStore

            store = MemoryStore(MemoryDatabase(memory_db))
            store.initialize()
            counts["memory"] = {
                "sessions": len(store.list_sessions(limit=1000)),
                "memories": len(store.memories.list(include_deleted=True, limit=1000)),
                "history_hits": len(store.search_history("答复", limit=5)),
            }
        payload = {
            "backup": str(backup),
            "restored_from_manifest": manifest.get("counts") or {},
            "restored_counts": counts,
            "memory_integrity": result["memory_integrity"],
            "ok": result["memory_integrity"] == "ok"
            and result.get("tombstones") is not None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
