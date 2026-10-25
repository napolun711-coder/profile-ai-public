"""本机资料库视图（Phase 16 P0）：把「知识库里的文档」与「本机的作业档案」对上号。

为什么需要它：
    知识库（LightRAG）只认文档名（`file_path` = 我们上传时的 `<文件名主干>.md`），
    而用户真正关心的是「这份资料的原件在哪、能不能双击打开、留档在哪」。
    本模块负责把两侧拼成一条完整的记录：

    1. 扫 `data/parsed/*/result.json` 得到本机作业清单（file_id ↔ 原文件名 ↔ 各路径）；
    2. 把上游文档的 `file_path` 映射回本地作业 —— 两条路：
       · 先查 `data/gallery/index.json`（Phase 17：图片资料的文档名是用户自己起的，
         与文件名毫无关系，只能靠这份显式索引对上）；
       · 再按**文件名主干的小写形式**比对（上传时 `<stem>.md` 经过 `safe_filename`
         清洗，两侧用同一套规则即可对齐 —— Phase 16 的老规则，其余资料照旧）；
    3. 组装给网页用的「一条文档」：上游状态 + 本机档案 + 原件可用性。

边界（与 ingest.py 的分工）：
    · 本模块**只读**，不改任何文件；写操作（解析 / 入库 / 留档）全在 ingest.py；
    · 不联网、不判定 —— 上游数据由调用方（routers/files.py）取好传进来；
    · 目录常量全部取自 ingest（`ingest.PARSED_DIR` 等），因此单测里 patch ingest 的
      目录就能整块换到临时目录，不需要另开一套桩。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.paths import KNOWLEDGE_DIR, safe_filename
from app.services import gallery, ingest

logger = logging.getLogger(__name__)

#: 本机作业状态 → 人话（直接复用 ingest 的表，避免同一状态两种说法）
STATE_LABELS = ingest.STATE_LABELS

#: 上游文档状态 → 人话（LightRAG 1.5.6 的 DocStatus 全集）
UPSTREAM_STATUS_LABELS: Dict[str, str] = {
    "pending": "排队中",
    "parsing": "解析中",
    "analyzing": "分析中",
    "processing": "建索引中",
    "preprocessed": "预处理完成",
    "processed": "已完成",
    "failed": "处理失败",
}


def upstream_status_label(status: str) -> str:
    text = str(status or "").strip().lower()
    return UPSTREAM_STATUS_LABELS.get(text, text or "未知")


def stem_key(name: str) -> str:
    """文件名 → 比对键：去掉目录与扩展名、清洗成安全单段、转小写。

    上传时上游拿到的名字是 `safe_filename(<主干>) + ".md"`（见 ingest._stage_ingest），
    本地档案里的 `name` 是用户原始文件名；两边都用这一套规则收敛后才能对上。
    空名字返回空串（**不是** safe_filename 的兜底名）—— 那是「没有可比的键」。
    """
    base = Path(str(name or "")).name
    if not base:
        return ""
    stem = Path(base).stem
    if not stem.strip(" ."):
        return ""
    return safe_filename(stem).lower()


def markdown_name_of(name: str) -> str:
    """本地原始文件名 → 上游那份 Markdown 的文档名（`<stem>.md`）。"""
    return f"{safe_filename(Path(str(name or 'document')).stem) or 'document'}.md"


def doc_name_of(data: Dict[str, Any]) -> str:
    """这份作业当前在知识库里的文档名。

    规则只有一份（`ingest.doc_name_for`）：图片资料用用户起的名字
    （`<名字>_<短ID>.md`），其余资料还是 `<原文件名主干>.md`。这里转发，不另立一套。
    """
    return ingest.doc_name_for(data)


# ============================================================
# 本机作业档案
# ============================================================

def _record(data: Dict[str, Any]) -> Dict[str, Any]:
    """把一份 result.json 压成页面要用的精简记录（不含正文，避免接口过大）。"""
    file_id = str(data.get("file_id") or "")
    quality = data.get("quality") or {}
    metrics = quality.get("metrics") or {}
    lightrag = data.get("lightrag") or {}
    state = str(data.get("state") or "")
    return {
        "file_id": file_id,
        "name": str(data.get("name") or ""),
        # Phase 17：知识库里的文档名 —— 图片资料是**用户起的名字**（`书桌照片_3fcb99.md`），
        # 不是「原文件名主干.md」，别再拿 `name` 去推算（推算的结果只对非图片资料成立）
        "markdown_name": doc_name_of(data),
        "ext": str(data.get("ext") or ""),
        "size": int(data.get("size") or 0),
        "sha256": str(data.get("sha256") or ""),
        "route": str(data.get("route") or ""),
        "route_label": str(data.get("route_label") or ""),
        "origin": str(data.get("origin") or "upload"),        # upload / inbox
        "state": state,
        "state_label": STATE_LABELS.get(state, state or "未知"),
        "detail": str(data.get("detail") or ""),
        "quality_level": str(quality.get("level") or ""),
        "content_chars": int(metrics.get("char_total") or 0),
        "knowledge_path": str(data.get("knowledge_path") or ""),
        "archived_path": str(data.get("archived_path") or ""),
        "source_path": str(data.get("source_path") or ""),
        "track_id": str(lightrag.get("track_id") or ""),
        "lightrag_state": str(lightrag.get("state") or ""),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def local_records() -> List[Dict[str, Any]]:
    """本机所有作业档案（按受理时间倒序；目录不存在 / 档案损坏都当没有）。"""
    try:
        directories = [path for path in ingest.PARSED_DIR.iterdir() if path.is_dir()]
    except OSError as exc:            # 目录还不存在（一次都没上传过）也走这里
        logger.debug("读取解析目录失败（当作没有作业）：%s", exc)
        return []

    records: List[Dict[str, Any]] = []
    for directory in directories:
        data = ingest.read_result(directory.name)
        if not data:
            continue
        records.append(_record(data))

    records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return records


def record_for(file_id: str, records: Optional[Iterable[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """按 file_id 取一条本机记录（`records` 传现成的清单可以少扫一次目录）。"""
    wanted = str(file_id or "").strip()
    if not wanted:
        return None
    for item in records if records is not None else local_records():
        if str(item.get("file_id") or "") == wanted:
            return item
    return None


def build_index(records: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """文件名主干 → 本机记录列表（同一主干可能对应多次上传）。"""
    index: Dict[str, List[Dict[str, Any]]] = {}
    for item in records if records is not None else local_records():
        key = stem_key(str(item.get("name") or ""))
        if key:
            index.setdefault(key, []).append(item)
    return index


def newest(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """同一主干有多条档案时取最近受理的那条。"""
    if not items:
        return None
    return sorted(items, key=lambda item: str(item.get("created_at") or ""), reverse=True)[0]


def match_document(
    file_path: str = "",
    file_id: str = "",
    *,
    index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    """把上游文档映射回本机档案。

    顺序：先按 file_id（本地列表里的行直接带过来），再按 `file_path` 的文件名主干。
    对不上就是「这条文档不是本应用上传的」（历史上用 LightRAG 自带页面传过、
    或者换了机器 / 换了 data 目录），页面会如实说明「找不到本机记录」。
    """
    lookup = index if index is not None else build_index()

    if file_id:
        for items in lookup.values():
            for item in items:
                if str(item.get("file_id") or "") == str(file_id):
                    return item

    # Phase 17：图片资料的文档名是用户起的（`书桌照片_3fcb99.md`），与文件名彻底无关，
    # 只能靠 `data/gallery/index.json` 这份显式索引对上；索引丢了/坏了会从各作业档案重建。
    owner = gallery.owner_of(file_path)
    if owner:
        for items in lookup.values():
            for item in items:
                if str(item.get("file_id") or "") == owner:
                    return item

    key = stem_key(file_path)
    if not key:
        return None
    return newest(lookup.get(key, []))


def knowledge_files() -> List[Path]:
    """knowledge/ 里的留档文件（留档规则见 ingest._save_knowledge）。"""
    try:
        return sorted(path for path in KNOWLEDGE_DIR.glob("*") if path.is_file())
    except OSError as exc:
        logger.debug("读取知识库留档目录失败：%s", exc)
        return []


def archive_name_for(record: Dict[str, Any]) -> str:
    """这条记录在 knowledge/ 里的留档文件名（不读盘，只按规则拼）。

    兼容两种输入：`library.local_records()` 的视图（带 `markdown_name`）与
    直接读出来的 result.json（只有 `name`）—— 原件定位两条路径都会用到。
    """
    file_id = safe_filename(str(record.get("file_id") or ""))
    if not file_id:
        return ""
    markdown_name = (
        str(record.get("markdown_name") or "")
        or str(record.get("doc_name") or "")
        or markdown_name_of(str(record.get("name") or ""))
    )
    return f"{file_id}_{markdown_name}"


# ============================================================
# 上游不可用时的降级视图
# ============================================================

def local_only_documents() -> List[Dict[str, Any]]:
    """知识库连不上时，页面至少还能看到「本机处理过什么」。"""
    items: List[Dict[str, Any]] = []
    for record in local_records():
        items.append(
            {
                "doc_id": "",
                "file_path": record.get("markdown_name") or "",
                "status": "",
                "status_label": "知识库未连接",
                "chunks_count": None,
                "content_length": record.get("content_chars") or 0,
                "created_at": record.get("created_at") or "",
                "updated_at": record.get("updated_at") or "",
                "record": record,
                "error_msg": "",
                "track_id": record.get("track_id") or "",
            }
        )
    return items
