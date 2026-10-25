"""M6：SQLite 在线备份 / 恢复 / 墓碑重放。

边界：
- 只用 SQLite `Connection.backup`，不直接复制运行中的 DB/WAL；
- 备份目录包含 memory.db、deletions.jsonl、manifest.json；
- 恢复必须先停服（CLI 检查），先备份当前库，再替换、重放当前墓碑、校验；
- 只写 ID/计数/哈希，不把正文写到日志。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.paths import BACKUP_DIR, MEMORY_DIR
DEFAULT_BACKUP_ROOT = BACKUP_DIR
DEFAULT_TOMBSTONES = MEMORY_DIR / "deletions.jsonl"
MANIFEST_NAME = "manifest.json"
MEMORY_NAME = "memory.db"
TOMBSTONES_NAME = "deletions.jsonl"
BACKUP_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> str:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0] if row else "unknown")
    finally:
        conn.close()


def _counts(path: Path) -> Dict[str, int]:
    result: Dict[str, int] = {}
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
            )
        ]
        for table in tables:
            try:
                result[table] = int(
                    conn.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0]
                )
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return result


def _online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target), timeout=30)
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    status = _integrity(target)
    if status != "ok":
        raise RuntimeError("备份库完整性检查失败：%s" % status)


def _file_entry(path: Path) -> Dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def backup_all(
    *,
    memory_db: Path,
    tombstones: Path = DEFAULT_TOMBSTONES,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    keep: int = 14,
) -> Dict[str, Any]:
    """创建一份带 manifest 的备份，并轮换旧备份。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    directory = backup_root / ("backup-" + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    files: Dict[str, Any] = {}
    counts: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    for name, source in [(MEMORY_NAME, memory_db)]:
        if not Path(source).is_file():
            continue
        target = directory / name
        _online_backup(Path(source), target)
        files[name] = _file_entry(target)
        counts[name] = _counts(target)
        sources[name] = str(Path(source))

    tombstone_source = Path(tombstones)
    if tombstone_source.is_file():
        target = directory / TOMBSTONES_NAME
        shutil.copy2(tombstone_source, target)
        files[TOMBSTONES_NAME] = _file_entry(target)
        sources[TOMBSTONES_NAME] = str(tombstone_source)

    manifest = {
        "version": BACKUP_VERSION,
        "created_at": _utc_now(),
        "files": files,
        "counts": counts,
        "sources": sources,
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _rotate(backup_root, keep)
    return {"directory": str(directory), "manifest": manifest}


def _rotate(root: Path, keep: int) -> None:
    try:
        limit = max(1, int(keep))
    except (TypeError, ValueError):
        limit = 14
    dirs = sorted(
        [
            path
            for path in root.iterdir()
            if path.is_dir() and (path / MANIFEST_NAME).is_file()
        ],
        key=lambda path: path.name,
    )
    for old in dirs[:-limit]:
        shutil.rmtree(old, ignore_errors=True)


def verify_backup(directory: Path) -> Dict[str, Any]:
    path = Path(directory)
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError("备份缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("version") or 0) != BACKUP_VERSION:
        raise ValueError("不支持的备份版本")
    for name, entry in (manifest.get("files") or {}).items():
        target = path / name
        if not target.is_file():
            raise ValueError("备份缺少文件：%s" % name)
        if int(entry.get("bytes") or -1) != target.stat().st_size:
            raise ValueError("备份文件大小不一致：%s" % name)
        if str(entry.get("sha256") or "") != _sha256(target):
            raise ValueError("备份文件哈希不一致：%s" % name)
        if target.suffix == ".db" and _integrity(target) != "ok":
            raise ValueError("备份库完整性检查失败：%s" % name)
    return manifest


def _replace_db(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".restore-tmp")
    shutil.copy2(source, temp)
    if _integrity(temp) != "ok":
        temp.unlink(missing_ok=True)
        raise RuntimeError("待恢复库完整性检查失败：%s" % source.name)
    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists():
            side.unlink()
    os.replace(temp, target)


def _read_tombstones(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _merge_tombstones(current: Path, backup: Optional[Path]) -> List[str]:
    current_lines = _read_tombstones(current)
    backup_lines = _read_tombstones(backup) if backup else []
    seen = set(current_lines)
    merged = list(current_lines)
    missing = [line for line in backup_lines if line not in seen]
    if missing:
        current.parent.mkdir(parents=True, exist_ok=True)
        with current.open("a", encoding="utf-8") as handle:
            for line in missing:
                handle.write(line + "\n")
        merged.extend(missing)
    return merged


def _fts_delete_memory(conn: sqlite3.Connection, memory_id: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if not exists:
        conn.execute("UPDATE memories SET fts_indexed = 0 WHERE id = ?", (memory_id,))
        return
    rows = conn.execute(
        "SELECT rowid, subject, content, keywords FROM memories "
        "WHERE id = ? AND fts_indexed = 1",
        (memory_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO memories_fts(memories_fts, rowid, subject, content, keywords) "
            "VALUES ('delete', ?, ?, ?, ?)",
            (row["rowid"], row["subject"], row["content"], row["keywords"]),
        )
    conn.execute("UPDATE memories SET fts_indexed = 0 WHERE id = ?", (memory_id,))


def _fts_delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if not exists:
        conn.execute(
            "UPDATE messages SET fts_indexed = 0 WHERE session_id = ?", (session_id,)
        )
        return
    rows = conn.execute(
        "SELECT rowid, content FROM messages WHERE session_id = ? AND fts_indexed = 1",
        (session_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', ?, ?)",
            (row["rowid"], row["content"]),
        )
    conn.execute(
        "UPDATE messages SET fts_indexed = 0 WHERE session_id = ?", (session_id,)
    )


def replay_tombstones(memory_db: Path, records: Iterable[str]) -> Dict[str, int]:
    """把当前墓碑应用到恢复后的记忆库；只处理 ID/模式，不读正文。"""
    parsed = []
    for line in records:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get("kind") and item.get("id"):
            parsed.append(item)
    counts = {"memory": 0, "session": 0, "session_messages": 0, "message": 0, "all": 0, "skipped": 0}
    conn = sqlite3.connect(str(memory_db), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_suppressions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, "
            "message_id TEXT, session_id TEXT, quote_hash TEXT, "
            "reason TEXT NOT NULL DEFAULT 'forget', created_at TEXT NOT NULL)"
        )
        for item in parsed:
            kind = str(item.get("kind"))
            item_id = str(item.get("id"))
            mode = str(item.get("mode") or "soft")
            if kind in ("memory", "forget"):
                _fts_delete_memory(conn, item_id)
                if mode == "purge" or kind == "forget":
                    conn.execute(
                        "UPDATE memory_events SET before_json = NULL, after_json = NULL "
                        "WHERE memory_id = ?",
                        (item_id,),
                    )
                    sources = conn.execute(
                        "SELECT message_id, session_id, quote FROM memory_sources "
                        "WHERE memory_id = ?",
                        (item_id,),
                    ).fetchall()
                    for source in sources:
                        quote = str(source["quote"] or "")
                        conn.execute(
                            "INSERT INTO memory_suppressions("
                            "memory_id, message_id, session_id, quote_hash, reason, created_at"
                            ") VALUES (?, ?, ?, ?, 'forget', ?)",
                            (
                                item_id,
                                source["message_id"],
                                source["session_id"],
                                hashlib.sha256(quote.strip().lower().encode("utf-8")).hexdigest(),
                                _utc_now(),
                            ),
                        )
                    conn.execute("DELETE FROM memory_sources WHERE memory_id = ?", (item_id,))
                    conn.execute("DELETE FROM memories WHERE id = ?", (item_id,))
                else:
                    now = _utc_now()
                    conn.execute(
                        "UPDATE memories SET status = 'deleted', deleted_at = ?, "
                        "updated_at = ?, version = version + 1 WHERE id = ?",
                        (now, now, item_id),
                    )
                counts["memory"] += 1
            elif kind == "session":
                _fts_delete_session(conn, item_id)
                if mode == "purge":
                    conn.execute("DELETE FROM messages WHERE session_id = ?", (item_id,))
                    conn.execute("DELETE FROM sessions WHERE id = ?", (item_id,))
                else:
                    now = _utc_now()
                    conn.execute(
                        "UPDATE sessions SET status = 'deleted', deleted_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (now, now, item_id),
                    )
                counts["session"] += 1
            elif kind == "session_messages":
                ids = item.get("message_ids") or []
                for message_id in ids:
                    conn.execute("DELETE FROM messages WHERE id = ?", (str(message_id),))
                counts["session_messages"] += 1
            elif kind == "message":
                conn.execute("DELETE FROM messages WHERE id = ?", (item_id,))
                counts["message"] += 1
            elif kind == "all":
                for table in (
                    "memory_sources",
                    "memory_events",
                    "memory_suppressions",
                    "memories",
                    "memory_jobs",
                    "session_memory_state",
                    "messages",
                    "sessions",
                    "topics",
                ):
                    try:
                        conn.execute('DELETE FROM "%s"' % table)
                    except sqlite3.Error:
                        pass
                counts["all"] += 1
            else:
                counts["skipped"] += 1
        conn.execute(
            "UPDATE sessions SET message_count = "
            "(SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id)"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return counts


def restore_backup(
    directory: Path,
    *,
    memory_db: Path,
    tombstones: Path = DEFAULT_TOMBSTONES,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    keep: int = 14,
) -> Dict[str, Any]:
    """恢复备份：校验 → 备份当前库 → 替换 → 合并墓碑并重放 → 校验。"""
    path = Path(directory)
    manifest = verify_backup(path)
    pre = backup_all(
        memory_db=memory_db,
        tombstones=tombstones,
        backup_root=backup_root,
        keep=keep,
    )
    memory_source = path / MEMORY_NAME
    try:
        if memory_source.is_file():
            _replace_db(memory_source, Path(memory_db))
        backup_tombstones = path / TOMBSTONES_NAME
        merged = _merge_tombstones(
            Path(tombstones),
            backup_tombstones if backup_tombstones.is_file() else None,
        )
        replay = replay_tombstones(Path(memory_db), merged) if memory_source.is_file() else {}
    except Exception:
        # 恢复中途失败：用恢复前备份把当前库拉回来，避免半恢复状态。
        pre_memory = Path(pre["directory"]) / MEMORY_NAME
        try:
            if pre_memory.is_file():
                _replace_db(pre_memory, Path(memory_db))
        except Exception:  # noqa: BLE001 - 回滚失败时保留原始异常
            pass
        raise
    return {
        "restored_from": str(path),
        "pre_restore_backup": pre["directory"],
        "manifest": manifest,
        "tombstones": replay,
        "memory_integrity": _integrity(Path(memory_db)) if Path(memory_db).is_file() else "missing",
    }


__all__ = [
    "DEFAULT_BACKUP_ROOT",
    "DEFAULT_TOMBSTONES",
    "backup_all",
    "replay_tombstones",
    "restore_backup",
    "verify_backup",
]
