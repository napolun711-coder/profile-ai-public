"""原件定位（Phase 16 P1）：从「知识库里的文档」找到「本机上的原件」。

用户双击参考资料时想看的是**原件**（原始 PDF / docx / 照片），而不是解析后的
Markdown。上游只给我们一个文档名（`file_path` = `<主干>.md`），原件在本机的哪
里要靠这里按顺序找（每一步都要求文件真实存在，命中即返回）：

    1. 档案里的 `archived_path` —— 上传/轮询留档的原件
       （`data\\uploads\\日期\\`、`data\\originals\\YYYY-MM\\`，两个入口共用同一字段）；
    2. 档案里的 `source_path` —— 解析时抄的那份原件副本
       （`data\\parsed\\<file_id>\\source.<ext>`；删了解析目录就没了，所以排第二）；
    3. `data\\originals\\` 里按文件名精确搜（轮询入库的原件都在这儿，按月份分目录）；
    4. `data\\uploads\\` 里按文件名搜（网页上传的原件留档，名字带时间戳前缀）；
    5. `knowledge\\<file_id>_<主干>.md` —— 解析后的正文（**不是原件**，明确标注）；
    6. 都没有 → `available=False` + 一句「为什么打不开、还能怎么办」。

安全边界（`safe_path`）：
    只允许读 `data\\`、`knowledge\\`、`output\\` 里的**已存在文件**，其余一律拒绝
    （`C:\\Windows\\win.ini`、`..\\..` 这类都会被挡掉），所以 `/api/files/raw`
    即使收到别人构造的 path 也读不到项目之外的东西。

本模块纯读盘、不联网、不改文件。
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from app.core.paths import (
    BASE_DIR,
    DATA_DIR,
    KNOWLEDGE_DIR,
    ORIGINALS_DIR,
    OUTPUT_DIR,
    UPLOAD_DIR,
)
from app.services import library as library_service

logger = logging.getLogger(__name__)

#: `/api/files/raw` 允许读的根目录（其余一律拒绝）
ALLOWED_ROOTS: Tuple[Path, ...] = (DATA_DIR, KNOWLEDGE_DIR, OUTPUT_DIR)

#: 搜索原件时最多往下一层找（originals 按 YYYY-MM 分月、uploads 按 YYYYMMDD 分日）
SEARCH_DEPTH = 2

#: 原件的来源类型 → 页面上的人话
KIND_LABELS: Dict[str, str] = {
    "archive": "原件留档",
    "source": "原件副本",
    "original": "原件",
    "knowledge": "解析后的正文",
}

#: 双击打开时给浏览器的说明（解析文本要提示「这不是原件」）
KIND_NOTES: Dict[str, str] = {
    "knowledge": "这是解析后的正文（.md），原件不在本机；如需原件请找原始文件重新放进 data\\inbox\\。",
}


def _under(child: Path, root: Path) -> bool:
    """child 是否在 root 之内（两侧都先解析成绝对路径，挡掉 ..\\.. 这类构造）。"""
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


#: 已经警告过的越界路径（同一个路径只警告一次，避免日志被刷满）
_OUT_OF_SCOPE_SEEN: set = set()

#: 用来识别「本项目的历史目录写法」的片段（只影响日志级别，不参与安全判断）
PROJECT_SEGMENTS = ("/data/", "/knowledge/", "/output/", "/rag_storage/")


def _looks_like_project_path(text: str) -> bool:
    """这条路径看着是不是「本项目里的目录」？

    项目搬过家（`C:\\app\\data\\…` → `<project>\\data\\…`）时，旧档案里的绝对路径
    会落在允许范围之外。那不是安全事件，而是历史数据 —— `resolve()` 会按文件名回退查找，
    所以这种情况只记 debug；真正指向系统目录（`C:\\Windows\\…`）的才值得警告。
    """
    lowered = text.replace("\\", "/").lower()
    return any(segment in lowered for segment in PROJECT_SEGMENTS)


def safe_path(raw: Any) -> Optional[Path]:
    """把「档案里记的路径」变成本机上的真实文件路径；不在允许范围内就返回 None。

    接受绝对路径与相对项目根的路径（result.json 里两种都出现过：老版本记的是
    相对路径 `data/uploads/...`，新版本记的是绝对路径）。

    越界一律返回 None（安全边界不放宽）：项目搬过家的旧绝对路径只记 debug 且每路径一次，
    剩下的交给 `resolve()` 按文件名在 `data\\originals\\` / `data\\uploads\\` 里回退查找。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    try:
        resolved = candidate.resolve()
    except OSError:                       # 路径里有非法字符 / 太长
        return None
    if not any(_under(resolved, root.resolve()) for root in ALLOWED_ROOTS):
        if _looks_like_project_path(text):
            logger.debug("档案里的路径已不在当前项目内（按文件名回退查找）：%s", resolved)
        else:
            key = str(resolved).lower()
            if key not in _OUT_OF_SCOPE_SEEN:
                if len(_OUT_OF_SCOPE_SEEN) > 200:      # 兜底：别让这个集合一直长大
                    _OUT_OF_SCOPE_SEEN.clear()
                _OUT_OF_SCOPE_SEEN.add(key)
                logger.warning("拒绝访问允许范围之外的路径：%s", resolved)
        return None
    return resolved if resolved.is_file() else None


def relative_path(path: Path) -> str:
    """项目内的相对路径（页面提示用；不在项目里就回绝对路径）。"""
    try:
        return str(path.relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        return str(path)


def _info(path: Path, kind: str) -> Dict[str, Any]:
    """文件 → 页面要用的信息（打不开的文件也要能说清「它在哪、多大」）。"""
    try:
        stat = path.stat()
        size = int(stat.st_size)
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        size, mtime = 0, ""
    return {
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "name": path.name,
        "path": str(path),
        "relative": relative_path(path),
        "ext": path.suffix.lower(),
        "size": size,
        "mtime": mtime,
        "note": KIND_NOTES.get(kind, ""),
    }


def search_in(root: Path, wanted: str) -> Optional[Path]:
    """在 root（含下一层子目录）里按文件名找文件。

    匹配规则（宽松但确定）：名字完全一样，或 `endswith "_<名>"`。
    上传留档的名字是 `<时间戳>_<原名>`、轮询留档在 `YYYY-MM\\` 下保留原名，
    两种都能命中。同一名字有多个时取**最近修改**的那个（通常是我们要找的）。
    """
    name = Path(str(wanted or "")).name.strip()
    if not name or not root.is_dir():
        return None

    lowered = name.lower()
    hits: List[Path] = []
    queue: List[Tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        try:
            entries = list(directory.iterdir())
        except OSError:                      # 权限 / 目录被删：跳过
            continue
        for entry in entries:
            if entry.is_dir():
                if depth + 1 < SEARCH_DEPTH:
                    queue.append((entry, depth + 1))
                continue
            current = entry.name.lower()
            if current == lowered or current.endswith("_" + lowered):
                hits.append(entry)

    if not hits:
        return None
    return sorted(hits, key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)[0]


def _missing(record: Optional[Dict[str, Any]], searched: Sequence[str]) -> Dict[str, Any]:
    """找不到原件时，如实说明「为什么」与「还能怎么办」。"""
    names = [name for name in searched if name]
    if record:
        subject = f"「{record.get('name') or record.get('file_id')}」"
        reason = (
            f"没找到{subject}的原件：本机既没有上传留档（data\\uploads）、"
            "也没有原件留档（data\\originals）。可能是在别的机器 / 别的 data 目录里入库的，"
            "或者原件已被移动、删除。"
        )
        if record.get("knowledge_path"):
            reason += "解析后的正文还在 knowledge\\ 里，可以按「解析文本」查看。"
    else:
        reason = (
            "这条文档不是本应用上传的（找不到对应的本机记录）："
            "它可能来自 LightRAG 自带页面或别的工具，因此本机没有它的原件。"
        )
    return {
        "available": False,
        "kind": "",
        "kind_label": "",
        "name": names[0] if names else "",
        "path": "",
        "relative": "",
        "ext": Path(names[0]).suffix.lower() if names else "",
        "size": 0,
        "mtime": "",
        "note": "",
        "reason": reason,
    }


def resolve(
    record: Optional[Dict[str, Any]] = None,
    *,
    names: Iterable[str] = (),
) -> Dict[str, Any]:
    """按「解析顺序」找原件，返回 `{available, kind, path, url, reason, …}`。

    `record` 来自 `services/library.py`（本机作业档案，可为 None）；
    `names` 是额外的候选文件名（例如上游 `file_path` 去掉 .md 后的抄本）。
    """
    record = record or {}
    written = str(record.get("name") or "").strip() or str(record.get("markdown_name") or "").strip()
    searched: List[str] = []
    for candidate in [written, *names]:
        text = str(candidate or "").strip()
        if text and Path(text).name not in [Path(item).name for item in searched]:
            searched.append(Path(text).name)

    # 1) 档案里记的留档原件（最可靠：上传 / 轮询都会写这个字段）
    archived = safe_path(record.get("archived_path"))
    if archived is not None:
        return {**_info(archived, "archive"), "available": True, "reason": ""}

    # 2) 解析时抄的那份原件副本
    source = safe_path(record.get("source_path"))
    if source is not None:
        return {**_info(source, "source"), "available": True, "reason": ""}

    # 3) data\originals\（轮询入库的原件：按月分目录，保留原名）
    for name in searched:
        hit = search_in(ORIGINALS_DIR, name)
        if hit is not None:
            return {**_info(hit, "original"), "available": True, "reason": ""}

    # 4) data\uploads\（网页上传的原件留档：<时间戳>_<原名>）
    for name in searched:
        hit = search_in(UPLOAD_DIR, name)
        if hit is not None:
            return {**_info(hit, "archive"), "available": True, "reason": ""}

    # 5) knowledge\ 里的解析正文（不是原件，但至少能看到内容）
    archive_name = library_service.archive_name_for(record) if record else ""
    knowledge = safe_path(KNOWLEDGE_DIR / archive_name) if archive_name else None
    if knowledge is not None:
        return {**_info(knowledge, "knowledge"), "available": True, "reason": ""}

    return _missing(record, searched)


def resolve_for(
    *,
    file_id: str = "",
    file_path: str = "",
    record: Optional[Dict[str, Any]] = None,
    index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """`/api/files/raw` 与 `/api/files/resolve` 共用的入口：给 file_id 或文档名都能找到原件。

    `index` 是 `library.build_index()` 的结果：一次列表里要解析几十行时把它传进来，
    避免每行都重扫一遍 `data\\parsed\\`。
    """
    if record is None:
        record = library_service.match_document(file_path, file_id, index=index)
    names: List[str] = []
    if file_path:
        stem = Path(str(file_path)).stem
        if stem:
            names.append(stem)
    info = resolve(record, names=names)
    info["file_id"] = str((record or {}).get("file_id") or file_id or "")
    info["file_path"] = str(file_path or "")
    if record:
        info["name"] = info.get("name") or str(record.get("name") or "")
    # 有 file_id 就用它（稳定、不怕文档名重名）；否则按文档名找
    info["url"] = (
        raw_url(file_id=info["file_id"]) if info["file_id"] else raw_url(file_path=file_path)
    )
    return info


def raw_url(*, file_id: str = "", file_path: str = "", path: str = "") -> str:
    """`/api/files/raw` 的地址（页面双击打开、前端探测都用它，参数集中在一处）。"""
    if file_id and not file_path:
        return "/api/files/raw?file_id=" + quote(str(file_id), safe="")
    if path:
        return "/api/files/raw?path=" + quote(str(path), safe="")
    return "/api/files/raw?file_path=" + quote(str(file_path or ""), safe="")


def media_type_of(path: Path) -> str:
    """按扩展名猜 MIME（猜不出用二进制流；浏览器照样能下载）。"""
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"

