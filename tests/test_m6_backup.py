"""M6：在线备份、恢复、墓碑重放与轮换。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore
from app.services import m6_backup


class M6BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.memory_db = self.root / "memory.db"
        self.backup_root = self.root / "backups"
        self.store = MemoryStore(MemoryDatabase(self.memory_db))
        self.store.initialize()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backup(self, keep: int = 5):
        return m6_backup.backup_all(
            memory_db=self.memory_db,
            tombstones=self.store.tombstones.path,
            backup_root=self.backup_root,
            keep=keep,
        )

    def test_restore_replays_tombstones(self) -> None:
        session = self.store.create_session(title="项目背景资料集")
        self.store.add_user_message(session["id"], "我的项目背景从1979年开始，档案被销毁。")
        first = self._backup()

        # 备份后：删除项目背景会话、新增不存在于备份的会话。
        self.store.delete_session(session["id"])
        later = self.store.create_session(title="备份后的会话")
        self.store.add_user_message(later["id"], "这是备份之后的新内容。")
        result = m6_backup.restore_backup(
            Path(first["directory"]),
            memory_db=self.memory_db,
            tombstones=self.store.tombstones.path,
            backup_root=self.backup_root,
            keep=5,
        )
        self.assertEqual(result["memory_integrity"], "ok")

        restored = MemoryStore(MemoryDatabase(self.memory_db))
        restored.initialize()
        self.assertEqual(restored.search_history("项目背景", limit=10), [])
        self.assertIsNone(restored.get_session(later["id"]))
        self.assertIsNotNone(restored.get_session(session["id"], include_deleted=True))

    def test_rotation_keeps_latest(self) -> None:
        first = self._backup(keep=2)
        second = self._backup(keep=2)
        third = self._backup(keep=2)
        dirs = sorted(
            path.name for path in self.backup_root.iterdir() if path.is_dir()
        )
        self.assertEqual(len(dirs), 2)
        self.assertNotIn(Path(first["directory"]).name, dirs)
        self.assertIn(Path(second["directory"]).name, dirs)
        self.assertIn(Path(third["directory"]).name, dirs)

    def test_verify_rejects_tampered_backup(self) -> None:
        backup = self._backup()
        target = Path(backup["directory"]) / "memory.db"
        data = target.read_bytes()
        target.write_bytes(data[:-10] + b"tampered!!")
        with self.assertRaises(ValueError):
            m6_backup.verify_backup(Path(backup["directory"]))


if __name__ == "__main__":
    unittest.main()
