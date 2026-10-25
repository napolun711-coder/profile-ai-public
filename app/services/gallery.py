"""图片资料 ↔ 知识库文档的映射索引（Phase 17）。

为什么需要它：
    图片资料在知识库里用的是**用户自己起的名字**（`<名字>_<短ID>.md`），不再是
    「原文件名主干.md」——于是「上游文档名 → 本机哪份档案」这条对应关系不再能靠
    文件名推算（`library.stem_key` 那套巧合映射失效了）。这里维护一份显式映射，
    让「引用里双击打开原图」「按行删除」「重名守卫」三件事都还有确定的答案：

        data\\gallery\\index.json
        {"version": 2, "updated_at": "2026-10-16 12:00:00",
         "items": {"书桌照片_3fcb99.md": {"doc_name": "书桌照片_3fcb99.md",
                                          "file_id": "20261016120000-3fcb99fe",
                                          "name": "IMG_2026.jpg",
                                          "sha256": "3fcb99…", "at": "…",
                                          "evidence": {…见 services/evidence.py…}}}}

    v1 索引（没有 `evidence`）照常能读：缺的字段按空字典处理，读取时**不回写**。

    **索引是派生数据**：每份作业档案（`data\\parsed\\<file_id>\\result.json`）里都写着
    `doc_name` / `file_id`，所以索引丢了、写坏了都能 `rebuild()` 扫回来
    （`lookup()` 发现索引为空会顺手重建一次）——它绝不是「丢了就找不回原件」的单点。

约定：
    · 键（key）是文档名的小写形式（Windows 上文件名大小写不敏感）；
    · 只读写自己那个 json，不碰 `knowledge\\` / `data\\parsed\\` 里的业务文件；
    · 纯本地、不联网；出错一律降级（读不到当空、写不进只警告），不影响入库主流程。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services import evidence as evidence_service

logger = logging.getLogger(__name__)

INDEX_NAME = "index.json"
RESULT_NAME = "result.json"
#: v2：每条映射多一个 `evidence` 子字典（见 services/evidence.py）；v1 索引照常可读。
INDEX_VERSION = 2

#: 读改写索引的互斥锁（入库线程 + 网页请求线程都会写）
_lock = threading.Lock()


def _ingest_module():
    """延迟取 ingest 模块（ingest 顶层会导入本模块，所以这里不能顶层导入它）。"""
    from app.services import ingest as ingest_service

    return ingest_service


def parsed_dir() -> Path:
    """作业档案目录 —— **动态**取 `ingest.PARSED_DIR`：单测把它换到临时目录时，
    索引也跟着搬走，测试永远碰不到真实的 `data\\parsed\\`。"""
    return _ingest_module().PARSED_DIR


def gallery_dir() -> Path:
    """索引目录（`<data>\\gallery`，与作业档案同处一个 data 根下）。"""
    return parsed_dir().parent / "gallery"


def index_path() -> Path:
    """索引文件位置。"""
    return gallery_dir() / INDEX_NAME


def key_of(doc_name: str) -> str:
    """文档名的比对键：去掉目录、去首尾空白、转小写；空名字返回空串。"""
    return Path(str(doc_name or "")).name.strip().lower()


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _entry(
    doc_name: str,
    file_id: str,
    *,
    name: str = "",
    sha256: str = "",
    at: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """一条映射（存定位字段 + 证据；**不存整篇正文**）。"""
    return {
        "doc_name": str(doc_name or "").strip(),
        "file_id": str(file_id or "").strip(),
        "name": str(name or ""),
        "sha256": str(sha256 or ""),
        "at": at or _stamp(),
        "evidence": dict(evidence) if isinstance(evidence, dict) else {},
    }


def doc_name_of(data: Dict[str, Any]) -> str:
    """这份作业在上游的文档名。

    规则只有一份，在 `ingest.doc_name_for`（图片用用户命名、其余保持老规则）。
    """
    return _ingest_module().doc_name_for(data)


# ============================================================
# 读写索引
# ============================================================

def read_index() -> Dict[str, Any]:
    """读索引原文；文件不存在或损坏都返回空骨架（调用方按需重建，不抛异常）。"""
    path = index_path()
    if not path.is_file():
        return {"version": INDEX_VERSION, "updated_at": "", "items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("图片资料索引损坏或不可读（按空索引处理）：%s：%s", path, exc)
        return {"version": INDEX_VERSION, "updated_at": "", "items": {}}

    raw = data.get("items")
    if not isinstance(raw, dict):
        raw = {}
    items: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        item = dict(value)
        if not isinstance(item.get("evidence"), dict):
            # v1 条目没有证据：读的时候补空，**不回写文件**（读操作不该改磁盘）。
            item["evidence"] = {}
        items[str(key)] = item
    return {
        "version": int(data.get("version") or INDEX_VERSION),
        "updated_at": str(data.get("updated_at") or ""),
        "items": items,
    }


def write_index(items: Dict[str, Dict[str, Any]]) -> bool:
    """原子写索引（临时文件 + replace）；写不成只警告并返回 False（不阻断入库）。"""
    payload = json.dumps(
        {"version": INDEX_VERSION, "updated_at": _stamp(), "items": items},
        ensure_ascii=False,
        indent=2,
    )
    path = index_path()
    temp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
        return True
    except OSError as exc:
        logger.warning("写入图片资料索引失败（不影响入库）：%s：%s", path, exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:                                  # 临时文件删不掉不影响结果
            pass
        return False


def load() -> Dict[str, Dict[str, Any]]:
    """索引里的映射（读不到就是空字典）。"""
    return read_index()["items"]


# ============================================================
# 重建（派生数据总能扫回来）
# ============================================================

def records() -> List[Dict[str, Any]]:
    """扫 `data\\parsed\\*\\result.json`（只读档案，不读正文）。"""
    found: List[Dict[str, Any]] = []
    try:
        directories = [path for path in parsed_dir().iterdir() if path.is_dir()]
    except OSError as exc:
        logger.debug("读取解析目录失败（索引为空）：%s", exc)
        return found

    for directory in directories:
        target = directory / RESULT_NAME
        if not target.is_file():
            continue
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def _holds_a_name(data: Dict[str, Any]) -> bool:
    """这份档案该不该占着一个文档名（只有真进了知识库的才算，见 rebuild 的注释）。"""
    return str(data.get("state") or "") == _ingest_module().STATE_DONE


def rebuild(
    records_override: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """从各作业档案重建索引并落盘，返回新的映射。

    **只收进过知识库的作业**（`state=done`）：跳过 / 被拦 / 从知识库删除的作业在档案里
    也留着 `doc_name`，但它们在上游并不存在 —— 照抄进来的话名字会被它们白白锁住
    （真机表现：跳过一份图片之后再用同名入库，被「名字已经给了另一份资料」挡住）。
    """
    items: Dict[str, Dict[str, Any]] = {}
    source = list(records_override) if records_override is not None else records()
    for data in source:
        if not _holds_a_name(data):
            continue
        doc_name = doc_name_of(data)
        file_id = str(data.get("file_id") or "")
        key = key_of(doc_name)
        if not key or not file_id:
            continue
        items[key] = _entry(
            doc_name,
            file_id,
            name=str(data.get("name") or ""),
            sha256=str(data.get("sha256") or ""),
            at=str(data.get("updated_at") or data.get("created_at") or ""),
            evidence=evidence_service.from_job(data, doc_name=doc_name),
        )

    write_index(items)
    logger.info("图片资料索引已重建：%d 条（%s）", len(items), index_path())
    return items


def find_or_rebuild() -> Dict[str, Dict[str, Any]]:
    """读索引；**文件不在**（首次运行 / 被手动删掉）时扫 result.json 重建一次。

    只在「文件不在」时重建：索引文件存在但里面是空的，是一个**正常状态**（图片作业全被
    跳过或删除了）。以前把「空」当「丢了」处理，于是每次查询都把那些作业按档案重新登记
    回来 —— 名字被锁住、删除后仍显示占用，正是这个原因。
    """
    if index_path().is_file():
        return load()
    return rebuild()


# ============================================================
# 查询与登记
# ============================================================

def lookup(doc_name: str) -> Optional[Dict[str, Any]]:
    """按文档名取这条映射（大小写不敏感）；没有就返回 None。"""
    key = key_of(doc_name)
    if not key:
        return None
    entry = find_or_rebuild().get(key)
    return dict(entry) if entry else None


def owner_of(doc_name: str) -> str:
    """这个名字当前登记在哪个作业名下（没有就返回空串）。"""
    return str((lookup(doc_name) or {}).get("file_id") or "")


def register(
    doc_name: str,
    file_id: str,
    *,
    name: str = "",
    sha256: str = "",
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """入库成功后登记映射（同一个作业的旧名字会被一并移除，改名不会留下幽灵条目）。

    `evidence` 是 P1B 的证据 v1（`services/evidence.from_job` 的返回值）；
    老调用方不传时存空字典，索引仍可读。
    """
    key = key_of(doc_name)
    owner = str(file_id or "").strip()
    if not key or not owner:
        return load()

    with _lock:
        items = read_index()["items"]
        for stale in [
            item_key
            for item_key, value in items.items()
            if str(value.get("file_id") or "") == owner and item_key != key
        ]:
            items.pop(stale, None)
        items[key] = _entry(
            doc_name, owner, name=name, sha256=sha256, evidence=evidence
        )
        write_index(items)
    return items


def forget(file_id: str) -> Dict[str, Dict[str, Any]]:
    """删掉某个作业的映射（跳过 / 从知识库删除时调用；本来没有就是空操作）。"""
    owner = str(file_id or "").strip()
    if not owner:
        return load()

    with _lock:
        items = read_index()["items"]
        stale = [
            key for key, value in items.items() if str(value.get("file_id") or "") == owner
        ]
        if not stale:
            return items
        for key in stale:
            items.pop(key, None)
        write_index(items)
    logger.info("已从图片资料索引里移除：%s（%d 条）", owner, len(stale))
    return items
