"""文件夹轮询自动入库（Phase 16 P3）：把文件丢进 `data\\inbox\\` 就会自己入库。

一轮扫描做的事（每一轮都从零开始判断，服务重启后照样工作）：

    1. 跳过临时文件（`.tmp` / `.part` / `.crdownload`、`~$` 开头的 Office 锁文件、
       `.` 开头的隐藏文件）与 `_taken` / `_failed` 两个子目录；
    2. **等文件长稳**：大小与上一轮相同、且至少静置 `INBOX_SETTLE_SECONDS` 秒才处理
       （复制大文件要时间，急着读会拿到半截内容 —— 这是最容易踩的坑）；
    3. 算 sha256 → 查 `data\\watch\\index.json`：**受理过的不再受理第二次**
       （重复内容归档到 `data\\inbox\\_taken\\`，页面会说明「与某份文件内容相同」）；
    4. 原件**移动**到 `data\\originals\\YYYY-MM\\`（不是复制：inbox 立刻清空，不会重复扫描；
       原件也留在了一个好找的地方）；
    5. 交给 `services/ingest.py` 建作业并入队 —— 解析、质检、确认门、留档规则
       **与网页上传完全一致**（`INBOX_CONFIRM` 通过按作业覆盖生效，默认 auto）。

硬边界：
    · 本模块**不**直接调用 `lightrag.*`：入库的唯一出口仍然只有 `ingest._stage_ingest`；
    · 受理不了的文件（类型不支持 / 超出大小 / 读不了）移进 `data\\inbox\\_failed\\`
      并写一份同名 `.reason.txt`，**不删**（用户自己决定怎么处理）；
    · 一切状态只在内存里（事件环形缓冲 + 计数），唯一落盘的是指纹索引
      `data\\watch\\index.json`（防重复入库要用，丢了也只是可能重复受理一次）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import DEFAULT_INBOX_SETTLE_SECONDS, get_settings
from app.core.paths import (
    INBOX_DIR,
    INBOX_FAILED_DIR,
    INBOX_TAKEN_DIR,
    ORIGINALS_DIR,
    WATCH_DIR,
    WATCH_INDEX,
    safe_filename,
)
from app.services import ingest

logger = logging.getLogger(__name__)

#: 半成品 / 临时文件后缀：正在下载或正在写入的文件不碰，等它长稳再说
TEMP_SUFFIXES = (
    ".tmp", ".part", ".partial", ".crdownload", ".download", ".!ut", ".swp", ".lock", ".bak",
)

#: 以这些前缀开头的文件一律忽略（隐藏文件、Office 锁文件）
IGNORED_PREFIXES = (".", "~$")

#: 页面只显示最近这些条事件
EVENT_LIMIT = 50

#: 一轮最多受理多少个文件（避免有人一次丢进来几千个把服务压住）
BATCH_LIMIT = 20

#: 读取指纹时的块大小（大文件也不占内存）
CHUNK_BYTES = 1024 * 1024

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_seen: Dict[str, Tuple[int, float]] = {}     # 文件路径 → (大小, 首次看到的时间戳)
_events: List[Dict[str, Any]] = []
_started_at = ""
_last_scan = ""
_last_result: Dict[str, Any] = {}
_scans = 0
_accepted = 0
_duplicates = 0
_failed = 0
_index: Optional[Dict[str, Any]] = None

#: 本次进程运行的随机标识（会写进指纹索引的每一条记录）。
#: 用途见 `_should_reaccept()`：区分「同一次运行里重复投递」（该判重）与
#: 「上一次运行受理到一半就被中断」（该重新受理，不能把用户的资料静默吞掉）。
RUN_ID = secrets.token_hex(4)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> Dict[str, Path]:
    """建好轮询要用到的四个目录（幂等）。"""
    for path in (INBOX_DIR, INBOX_TAKEN_DIR, INBOX_FAILED_DIR, ORIGINALS_DIR, WATCH_DIR):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "inbox": INBOX_DIR,
        "taken": INBOX_TAKEN_DIR,
        "failed": INBOX_FAILED_DIR,
        "originals": ORIGINALS_DIR,
    }


def _relative(path: Path) -> str:
    """项目内相对路径（写进 result.json 的 archived_path 用的是这个形式）。"""
    from app.services import origin

    return origin.relative_path(path)


def _ignored(name: str) -> bool:
    """要忽略的文件吗（临时文件 / 隐藏文件 / Office 锁文件）。"""
    lowered = name.lower()
    if lowered.startswith(IGNORED_PREFIXES) or name.startswith(IGNORED_PREFIXES):
        return True
    return lowered.endswith(TEMP_SUFFIXES)


def fingerprint(path: Path) -> str:
    """文件内容的 sha256（流式读，大文件也不吃内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _record_event(kind: str, name: str, detail: str, *, file_id: str = "") -> None:
    """记一条事件（内存态，页面用；服务重启即清空）。"""
    with _lock:
        _events.insert(0, {
            "at": _now(),
            "kind": kind,            # accepted / duplicate / failed / skipped
            "name": name,
            "detail": detail,
            "file_id": file_id,
        })
        del _events[EVENT_LIMIT:]


# ============================================================
# 指纹索引（data\watch\index.json）：防重复入库
# ============================================================

def _load_index() -> Dict[str, Any]:
    """读指纹索引（进程内缓存；文件不存在 / 损坏都当空表）。"""
    global _index
    if _index is not None:
        return _index
    data: Dict[str, Any] = {}
    try:
        if WATCH_INDEX.is_file():
            # utf-8-sig：容忍别人（脚本 / 记事本 / PowerShell）写出的带 BOM 文件。否则
            # 整份索引会被当成「损坏」静默丢空，同一份资料又会重新入库（见 test_inbox.py）。
            raw = json.loads(WATCH_INDEX.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data = raw
    except (OSError, ValueError) as exc:
        logger.warning("指纹索引损坏 / 读不了（当作空表）：%s", exc)
    _index = data
    return _index


def _save_index(data: Dict[str, Any]) -> None:
    """写指纹索引（失败只记日志：最坏情况是重启后可能重复受理一次）。"""
    global _index
    _index = data
    try:
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        WATCH_INDEX.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("指纹索引写不进去（不影响本次受理）：%s", exc)


def known_fingerprints() -> int:
    """已受理过多少个指纹（页面显示用）。"""
    return len(_load_index())


# ============================================================
# 移动 / 隔离
# ============================================================

def unique_target(directory: Path, name: str) -> Path:
    """在目录里给 name 找一个不冲突的落点（重名加 `_2`、`_3`…）。"""
    cleaned = safe_filename(name) or "file"
    target = directory / cleaned
    if not target.exists():
        return target
    stem = Path(cleaned).stem
    suffix = Path(cleaned).suffix
    index = 2
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _move(path: Path, directory: Path) -> Path:
    """把文件移进目录（重名自动改名）；失败时返回原路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_target(directory, path.name)
    shutil.move(str(path), str(target))
    return target


def _isolate(path: Path, reason: str) -> Dict[str, Any]:
    """受理不了的文件：移进 `_failed` 并写一份同名 `.reason.txt`（**不删**）。"""
    global _failed
    try:
        target = _move(path, INBOX_FAILED_DIR)
    except OSError as exc:                 # 连移都移不动：留在原地，下一轮再说
        _record_event("failed", path.name, f"{reason}（而且移不进 _failed：{exc}）")
        with _lock:
            _failed += 1
        return {"name": path.name, "result": "failed", "reason": reason, "moved_to": ""}

    try:
        (target.parent / f"{target.name}.reason.txt").write_text(
            f"{_now()}\n{reason}\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("写失败原因文件失败：%s", exc)

    _record_event("failed", path.name, reason)
    with _lock:
        _failed += 1
    logger.warning("收件箱文件受理失败：%s | %s", path.name, reason)
    return {"name": path.name, "result": "failed", "reason": reason, "moved_to": str(target)}


def _extension_reason(name: str) -> str:
    """扩展名检查（与网页上传同一套 allowed_extensions）。"""
    allowed = get_settings().allowed_extensions
    if not allowed:
        return ""
    lowered = (name or "").lower()
    if any(lowered.endswith(ext.lower()) for ext in allowed):
        return ""
    return (
        f"不支持的文件类型：{name}。允许的扩展名：{', '.join(allowed)}"
        "（要加类型请到设置页改「允许的扩展名」）"
    )


def _size_reason(size: int) -> str:
    """大小检查（与网页上传同一套 max_upload_size）。"""
    limit = get_settings().max_upload_size
    if limit and size > limit:
        return f"文件太大：{size} 字节，超过上限 {limit} 字节（可到设置页改「上传大小上限」）"
    return ""


# ============================================================
# 受理一个文件
# ============================================================

def _should_reaccept(previous: Dict[str, Any]) -> Tuple[bool, str]:
    """重复内容该不该**重新受理**？返回 `(是否重新受理, 人话原因)`。

    判重的本意是「同一份内容别入库两次」，但**上一次受理到一半就被中断**
    （服务被停 / 进程被杀，正是 Phase 16 事故里的常态）时不能判重：
    否则用户把同一份文件再丢一次会被直接归档进 `_taken`，页面上只说「内容完全相同」，
    那份资料就**永远不会入库**了 —— 静默丢数据比多受理一次糟得多。

    判据刻意保守，只在确信「上次那件事已经不会自己继续了」时才重新受理：
        · 上一次没建成作业（file_id 为空）→ 重新受理；
        · 同一次运行内已经受理过（`run` 相同）→ 判重（用户重复投递，正确行为）；
        · 上一次是**别的运行**留下的、作业档案还停在本地解析阶段
          （parsing / checking / 空状态）→ 那次进程已经没了，解析不会继续 → 重新受理；
        · 其余（done / review / skipped / ingesting / failed / blocked / needs_ocr）
          → 判重：可能已经提交过知识库，重复受理会造出重复文档。
    """
    file_id = str(previous.get("file_id") or "")
    if not file_id:
        return True, "上一次受理没有建成作业"
    if str(previous.get("run") or "") == RUN_ID:
        return False, ""
    data = ingest.read_result(file_id)
    if not data:
        return True, "上一次的作业记录已不存在"
    state = str(data.get("state") or "")
    if state in (ingest.STATE_PARSING, ingest.STATE_CHECKING) or not state:
        label = ingest.STATE_LABELS.get(state) or state or "状态不明"
        return True, f"上一次受理后没跑完（{label}）"
    return False, ""


def _accept(path: Path, settings) -> Dict[str, Any]:
    """受理一个已长稳的文件：判重 → 检查 → 移进原件留档 → 建作业并入队。"""
    global _accepted, _duplicates, _failed

    # 1) 内容指纹判重
    try:
        digest = fingerprint(path)
    except OSError as exc:
        return _isolate(path, f"读不了这个文件：{exc}")

    index = _load_index()
    previous = index.get(digest)
    reaccept_reason = ""
    if previous:
        reaccept, why = _should_reaccept(previous)
        if not reaccept:
            try:
                moved = _move(path, INBOX_TAKEN_DIR)
            except OSError as exc:
                logger.warning("重复文件归档失败（留在收件箱，下一轮再试）：%s", exc)
                return {"name": path.name, "result": "duplicate", "reason": str(exc), "moved_to": ""}
            with _lock:
                _duplicates += 1
            detail = (
                f"内容与已受理过的《{previous.get('name') or '？'}》完全相同"
                f"（sha256 前 8 位 {digest[:8]}）：不再重复入库，已归档到 data\\inbox\\_taken\\"
            )
            _record_event("duplicate", path.name, detail, file_id=str(previous.get("file_id") or ""))
            logger.info("收件箱重复文件已归档：%s → %s", path.name, moved)
            return {
                "name": path.name,
                "result": "duplicate",
                "reason": detail,
                "moved_to": str(moved),
                "file_id": str(previous.get("file_id") or ""),
            }
        # 上一次受理没走完（多半是服务被停过）：这次重新受理，不判重
        reaccept_reason = why
        logger.info("收件箱重新受理：%s（%s）", path.name, why)

    # 2) 类型与大小（与网页上传完全同一套规则）
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _isolate(path, f"读不到文件大小：{exc}")
    for reason in (_extension_reason(path.name), _size_reason(size)):
        if reason:
            return _isolate(path, reason)

    # 3) 读进内存（读失败就别动文件，留在收件箱等下轮）
    try:
        content = path.read_bytes()
    except OSError as exc:
        return _isolate(path, f"读不了这个文件：{exc}")

    # 4) 原件**移动**到 data\originals\YYYY-MM\（inbox 立刻清空，不会重复扫描）
    month = ORIGINALS_DIR / datetime.now().strftime("%Y-%m")
    try:
        month.mkdir(parents=True, exist_ok=True)
        destination = unique_target(month, path.name)
        shutil.move(str(path), str(destination))
    except OSError as exc:
        return _isolate(path, f"原件归档失败（data\\originals）：{exc}")

    # 5) 建作业 + 入队（解析 / 质检 / 确认门 / 留档全在 ingest.py）
    relative = _relative(destination)
    try:
        job = ingest.create_job(
            filename=path.name,
            content=content,
            archived_path=relative,
            origin="inbox",
            confirm=str(getattr(settings, "inbox_confirm", "") or ""),
        )
        ingest.enqueue(job["file_id"])
    except Exception as exc:  # noqa: BLE001 - 受理失败也要留档与说明，不能把文件吞掉
        logger.exception("收件箱文件建作业失败：%s", path.name)
        index[digest] = {"name": path.name, "at": _now(), "file_id": "", "archived": relative}
        _save_index(index)
        try:
            moved = _move(destination, INBOX_FAILED_DIR)
            (moved.parent / f"{moved.name}.reason.txt").write_text(
                f"{_now()}\n受理失败：{exc}\n原件已归档到 {relative}\n", encoding="utf-8"
            )
        except OSError:
            pass
        _record_event("failed", path.name, f"建作业失败：{exc}")
        with _lock:
            _failed += 1
        return {"name": path.name, "result": "failed", "reason": str(exc), "moved_to": ""}

    index[digest] = {
        "name": path.name,
        "at": _now(),
        "file_id": job["file_id"],
        "archived": relative,
        "run": RUN_ID,                 # 哪一次运行受理的（判重时要用，见 _should_reaccept）
    }
    _save_index(index)
    with _lock:
        _accepted += 1
    detail = f"已受理（原件归档到 {relative}）：解析与质检在后台进行，详见「文件管理」页的作业状态"
    if reaccept_reason:
        detail = f"上次没入库，这次重新受理（{reaccept_reason}）。" + detail
    _record_event("accepted", path.name, detail, file_id=job["file_id"])
    logger.info("收件箱受理：%s → %s（file_id=%s）", path.name, destination, job["file_id"])
    return {
        "name": path.name,
        "result": "accepted",
        "file_id": job["file_id"],
        "state": job.get("state"),
        "archived": relative,
        "size": size,
        "reaccepted": bool(reaccept_reason),
        "reason": reaccept_reason,
    }


# ============================================================
# 扫描 / 状态 / 后台线程
# ============================================================

def _settle_seconds(settings) -> float:
    """「长稳」要等几秒（0 = 只要求连续两轮大小不变，不再额外等）。

    注意不能用 `value or 默认值`：`INBOX_SETTLE_SECONDS=0` 是合法取值（关掉额外等待），
    写成 `or` 会把它悄悄换成默认的 5 秒 —— 配置里写 0 却等 5 秒，很难查。
    """
    raw = getattr(settings, "inbox_settle_seconds", None)
    if raw is None:
        raw = DEFAULT_INBOX_SETTLE_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_INBOX_SETTLE_SECONDS)


def scan_once(*, reason: str = "poll", limit: int = BATCH_LIMIT) -> Dict[str, Any]:
    """扫一次收件箱，返回这一轮做了什么。

    · 每个文件必须**连续两轮大小不变**、且至少静置 `INBOX_SETTLE_SECONDS` 秒才受理
      （复制大文件要时间，急着读会拿到半截内容）；
    · 一轮最多受理 `limit` 个，剩下的下一轮继续（不长时间占住调用方）；
    · 单个文件出错不会中断本轮（它会被隔离并写下原因）。
    """
    global _last_scan, _last_result, _scans

    settings = get_settings()
    directories = ensure_dirs()
    inbox = directories["inbox"]
    settle = _settle_seconds(settings)
    now = time.time()

    with _lock:
        _scans += 1
        _last_scan = _now()

    # 已经被移走 / 删掉的候选从「等长稳」表里清掉
    for key in list(_seen):
        if not Path(key).is_file():
            _seen.pop(key, None)

    try:
        entries = sorted(entry for entry in inbox.iterdir() if entry.is_file())
    except OSError as exc:
        logger.warning("读收件箱失败（本轮跳过）：%s", exc)
        entries = []

    details: List[Dict[str, Any]] = []
    for entry in entries:
        if len(details) >= max(1, limit):
            break
        if _ignored(entry.name):
            continue
        key = str(entry)
        try:
            size = entry.stat().st_size
        except OSError:                    # 刚被移走：下一轮自然消失
            continue
        first = _seen.get(key)
        if first is None or first[0] != size:
            _seen[key] = (size, now)       # 刚出现 / 还在变大：下一轮再看
            continue
        if now - first[1] < settle:
            continue
        _seen.pop(key, None)
        try:
            details.append(_accept(entry, settings))
        except Exception as exc:           # noqa: BLE001 - 单个文件炸了不能带走整轮
            logger.exception("受理收件箱文件时出错：%s", entry.name)
            details.append({"name": entry.name, "result": "failed", "reason": str(exc)})

    settling = [
        {"name": Path(key).name, "size": item[0], "waited": int(now - item[1])}
        for key, item in _seen.items()
        if Path(key).is_file()
    ][:20]

    result = {
        "at": _now(),
        "reason": reason,
        "settle_seconds": int(settle),
        "inspected": len(entries),
        "settling": settling,
        "handled": details,
        "accepted": sum(1 for item in details if item.get("result") == "accepted"),
        "duplicate": sum(1 for item in details if item.get("result") == "duplicate"),
        "failed": sum(1 for item in details if item.get("result") == "failed"),
    }
    _last_result = result
    if details:
        logger.info(
            "收件箱扫描（%s）：受理 %d，重复 %d，失败 %d",
            reason, result["accepted"], result["duplicate"], result["failed"],
        )
    return result


def _dir_status(path: Path, *, recursive: bool = False) -> Dict[str, Any]:
    """目录概况（条数；`recursive=True` 时含子目录，例如 originals\\2026-10\\）。

    收件箱本身用**不递归**：`_taken` / `_failed` 是它的子目录，
    递归会把「已经归档的」算进「还等着处理的」，页面数字就假了。
    """
    files = 0
    truncated = False
    try:
        if path.is_dir():
            entries = list(path.rglob("*")) if recursive else list(path.iterdir())
            truncated = len(entries) > 2000
            files = sum(1 for entry in entries[:2000] if entry.is_file())
    except OSError:
        files = 0
    return {
        "path": str(path),
        "relative": _relative(path),
        "exists": path.is_dir(),
        "files": files,
        "truncated": truncated,
    }


def status() -> Dict[str, Any]:
    """轮询状态（页面与 `/api/inbox/status` 用）：只读、不扫盘、不改任何东西。"""
    settings = get_settings()
    inbox = _dir_status(INBOX_DIR)
    poll = int(getattr(settings, "inbox_poll_seconds", 10) or 10)
    with _lock:
        snapshot = {
            "scans": _scans,
            "accepted": _accepted,
            "duplicate": _duplicates,
            "failed": _failed,
            "started_at": _started_at,
            "last_scan": _last_scan,
            "settling": len(_seen),
        }
        events = list(_events)[:20]
    return {
        "enabled": bool(getattr(settings, "inbox_enabled", False)),
        "running": is_running(),
        "poll_seconds": poll,
        "settle_seconds": int(_settle_seconds(settings)),
        "confirm": str(getattr(settings, "inbox_confirm", "") or ""),
        "dirs": {
            "inbox": inbox,
            "taken": _dir_status(INBOX_TAKEN_DIR),
            "failed": _dir_status(INBOX_FAILED_DIR),
            "originals": _dir_status(ORIGINALS_DIR, recursive=True),
            "index": {
                "path": str(WATCH_INDEX),
                "relative": _relative(WATCH_INDEX),
                "exists": WATCH_INDEX.is_file(),
                "files": 1 if WATCH_INDEX.is_file() else 0,
            },
        },
        "waiting": inbox["files"],
        "known": known_fingerprints(),
        "last_result": _last_result,
        "events": events,
        "hint": (
            f"把文件复制或移动到 {inbox['relative']} 就会自动入库"
            f"（每 {poll} 秒扫一次；原件归档在 data\\originals\\）。"
        ),
        **snapshot,
    }


def is_running() -> bool:
    """后台线程还在跑吗。"""
    return _thread is not None and _thread.is_alive()


def _loop() -> None:
    """轮询线程：扫一轮 → 等 poll_seconds → 再扫（任何异常都不让线程退出）。"""
    while not _stop.is_set():
        settings = get_settings()
        try:
            scan_once(reason="poll")
        except Exception:                  # noqa: BLE001 - 轮询线程绝不能因一次异常就死
            logger.exception("文件夹轮询这一轮出错（已跳过，下一轮继续）")
        _stop.wait(max(3.0, float(getattr(settings, "inbox_poll_seconds", 10) or 10)))


def start() -> bool:
    """启动后台轮询（幂等；返回 True 表示这次真的启动了）。"""
    global _thread, _started_at

    settings = get_settings()
    if not getattr(settings, "inbox_enabled", False):
        logger.info("文件夹轮询未启用（INBOX_ENABLED=false）：data\\inbox 不会被扫描")
        return False

    ensure_dirs()
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _started_at = _now()
        _thread = threading.Thread(target=_loop, name="inbox-watcher", daemon=True)
        _thread.start()
    logger.info(
        "文件夹轮询已启动：%s（每 %s 秒扫一次，文件长稳 %s 秒后受理）",
        INBOX_DIR,
        getattr(settings, "inbox_poll_seconds", 10),
        getattr(settings, "inbox_settle_seconds", 5),
    )
    return True


def stop(timeout: float = 5.0) -> bool:
    """停掉后台轮询（服务关闭时调用；返回是否停干净）。"""
    global _thread
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    alive = thread is not None and thread.is_alive()
    _thread = None
    if alive:
        logger.warning("文件夹轮询线程没有在 %.1f 秒内退出（守护线程，不影响退出）", timeout)
    else:
        logger.info("文件夹轮询已停止")
    return not alive


def reset_state() -> None:
    """清掉内存态（候选、事件、计数、索引缓存）——**不动磁盘**，供单测使用。

    `RUN_ID` 也换一个：单测里用它模拟「服务重启」（新进程 = 新标识），
    这样「上次运行受理到一半就被停」这类场景才测得出来。
    """
    global _index, _started_at, _last_scan, _last_result, RUN_ID
    global _scans, _accepted, _duplicates, _failed
    with _lock:
        _seen.clear()
        _events.clear()
        _index = None
        _started_at = ""
        _last_scan = ""
        _last_result = {}
        _scans = _accepted = _duplicates = _failed = 0
        RUN_ID = secrets.token_hex(4)


