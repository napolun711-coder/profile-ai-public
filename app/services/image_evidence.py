"""P2C：图片证据第二通道 v1（只读、确定性、无第三方分词）。

它回答两个问题：

1. **本地通道**：gallery 索引 v2 里有哪些图片，和问题文字相关？
   - 只用索引里的 `caption` / `ocr_preview` / `keywords` / 文档名；
   - v1 条目（`evidence={}`）在**内存里**从 `result.json` 派生，不写索引；
   - 匹配用关键词与 2-gram 重叠，不引入分词依赖，结果可重复。

2. **检索通道**：`/query/data` 返回的 chunk / reference 里，哪些来自图片资料？
   - 按文档名查 gallery；
   - 同一张图片的多个 chunk 合并成一条证据；
   - 本地候选与检索候选按**各自配额**轮转合并，避免跨通道分数不可比。

边界：
    · 只读：不调用 `gallery.rebuild()` / `find_or_rebuild()`，不写索引、不写配置；
    · 不联网、不调用 LLM、不生成答案；
    · 不读取完整 OCR 正文；证据正文优先用检索 chunk，其次用 200 字预览；
    · 坏输入一律降级为空结果，不抛异常。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from app.plugins.contracts import Evidence
from app.services import evidence as evidence_service
from app.services import gallery

IMAGE_KIND = "image"
DEFAULT_LIMIT = 8
MAX_LIMIT = 20
LOCAL_QUOTA = 5
RETRIEVAL_QUOTA = 5
MAX_QUERY_TERMS = 32
MAX_CHUNK_IDS = 20
MAX_ASSET_PATHS = 20
MAX_KEYWORDS = 16

_SHORT_ID_RE = re.compile(r"_[0-9a-f]{6}$", re.IGNORECASE)
_WORD_SPLIT = re.compile(r"[^\w]+", re.UNICODE)
_CJK_RE = re.compile(
    r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯]"
)

#: 问题的“图片意图词”：匹配前先去掉，避免所有图片都因为“图片”二字命中。
GENERIC_CUES: Tuple[str, ...] = (
    "这张图",
    "那张图",
    "图片资料",
    "图片",
    "照片",
    "截图",
    "原图",
    "画面",
    "图里",
    "图中",
    "图像",
    "photo",
    "picture",
    "image",
    "screenshot",
)

#: 纯列举意图：没有内容词时允许返回全部图片。
LIST_CUES: Tuple[str, ...] = (
    "有哪些图片",
    "有哪些照片",
    "所有图片",
    "全部图片",
    "图片列表",
    "列出图片",
    "列出所有图片",
    "列出全部图片",
    "看看图片",
    "找图片",
    "搜图片",
    "所有照片",
    "列出照片",
)

#: 只过滤明显无意义的虚词/指代片段；宁可少过滤，也不引入词典依赖。
STOP_TERMS = frozenset(
    {
        "一下",
        "这个",
        "那个",
        "这些",
        "那些",
        "什么",
        "哪些",
        "怎么",
        "如何",
        "帮我",
        "看看",
        "找一",
        "找下",
        "里有",
        "里的",
        "有哪",
        "些项",
        "的图",
        "的照",
        "片里",
        "有什",
        "有什么",
        "里有什么",
        "列出",
        "列出所有",
        "所有",
    }
)


def _clamp_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any, *, limit: int) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    found: List[str] = []
    seen = set()
    for item in value:
        text = _text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(text)
        if len(found) >= limit:
            break
    return found


def _stem(name: str) -> str:
    """文档名 → 去除扩展名与末尾短 ID 的名字部分。"""
    base = Path(_text(name)).stem
    return _SHORT_ID_RE.sub("", base).strip(" ._-")


def _raw_url(file_id: str) -> str:
    """原图 URL；延迟导入 origin，避免 `import app.skills` 连带加载 ingest/library。"""
    try:
        from app.services.origin import raw_url

        return raw_url(file_id=file_id)
    except Exception:                                    # noqa: BLE001 - 只影响打开链接
        return "/api/files/raw?file_id=" + quote(str(file_id or ""), safe="")


def _gallery_meta(record: Dict[str, Any]) -> Dict[str, Any]:
    """Context Builder 认识的 gallery 子字典；不包含完整 OCR 正文。"""
    return {
        "kind": IMAGE_KIND,
        "caption": str(record.get("caption") or ""),
        "ocr_preview": str(record.get("ocr_preview") or ""),
        "asset_paths": list(record.get("asset_paths") or []),
        "keywords": list(record.get("keywords") or []),
        "result_pointer": str(record.get("result_pointer") or ""),
        "content_pointer": str(record.get("content_pointer") or ""),
    }


def _record(doc_name: str, file_id: str, item_evidence: Dict[str, Any]) -> Dict[str, Any]:
    image_id = _text(item_evidence.get("image_id"))
    return {
        "doc_name": doc_name,
        "file_id": file_id,
        "image_id": image_id,
        "caption": _text(item_evidence.get("caption")),
        "ocr_preview": _text(item_evidence.get("ocr_preview")),
        "asset_paths": _strings(item_evidence.get("asset_paths"), limit=MAX_ASSET_PATHS),
        "keywords": _strings(item_evidence.get("keywords"), limit=MAX_KEYWORDS),
        "result_pointer": _text(item_evidence.get("result_pointer")),
        "content_pointer": _text(item_evidence.get("content_pointer")),
        "origin_url": _raw_url(file_id),
    }


def _derive_evidence(file_id: str, doc_name: str) -> Dict[str, Any]:
    """v1 条目没有 evidence 时，在内存里从作业档案派生；**不写盘**。"""
    try:
        path = gallery.parsed_dir() / file_id / gallery.RESULT_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        derived = evidence_service.from_job(data, doc_name=doc_name)
    except Exception:                                    # noqa: BLE001 - 读不到/派生失败按无证据
        return {}
    return derived if isinstance(derived, dict) else {}


def image_records(items: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """从 gallery 索引（或注入的 items）取出图片记录；只读。"""
    if items is None:
        try:
            items = gallery.read_index().get("items")
        except Exception:                                # noqa: BLE001 - 索引坏了按空
            items = {}
    if not isinstance(items, dict):
        return []

    records: List[Dict[str, Any]] = []
    for key, entry in items.items():
        if not isinstance(entry, dict):
            continue
        doc_name = _text(entry.get("doc_name")) or _text(key)
        file_id = _text(entry.get("file_id"))
        if not doc_name or not file_id:
            continue

        item_evidence = entry.get("evidence")
        if not isinstance(item_evidence, dict) or not item_evidence:
            # 只对“没有证据”的 v1 条目派生；明确标成 document 的条目不猜。
            item_evidence = _derive_evidence(file_id, doc_name)
        if not isinstance(item_evidence, dict):
            continue
        if _text(item_evidence.get("kind")) != IMAGE_KIND:
            continue
        records.append(_record(doc_name, file_id, item_evidence))
    return records


def _loose_key(doc_name: str) -> str:
    """去掉末尾 `_<短ID>` 的文档名键。

    真机里存在历史作业：上游 `file_path` 是 `屏幕截图_2026-07-20_234254.md`，
    而本机 gallery 记录的是 `屏幕截图_2026-07-20_234254_b20c0f.md`。两者是同一份
    资料，只是短 ID 是后来补上的；精确匹配会漏掉这些历史文档。
    """
    key = gallery.key_of(doc_name)
    if not key:
        return ""
    path = Path(key)
    stem = _SHORT_ID_RE.sub("", path.stem).strip(" ._-")
    return f"{stem}{path.suffix}" if stem else ""


def image_map(items: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """文档名键 → 图片记录；供 `/query/data` 分类使用。

    精确键之外，只在**唯一**时补一个「去掉短 ID」的别名；两条图片记录共享同一
    别名时不补，避免把检索结果错配到另一张图。
    """
    records = image_records(items)
    mapping: Dict[str, Dict[str, Any]] = {}
    loose_counts: Dict[str, int] = {}

    for record in records:
        key = gallery.key_of(record["doc_name"])
        if key and key not in mapping:
            mapping[key] = record
        loose = _loose_key(record["doc_name"])
        if loose and loose != key:
            loose_counts[loose] = loose_counts.get(loose, 0) + 1

    for record in records:
        key = gallery.key_of(record["doc_name"])
        loose = _loose_key(record["doc_name"])
        if (
            loose
            and loose != key
            and loose_counts.get(loose) == 1
            and loose not in mapping
        ):
            mapping[loose] = record
    return mapping


# ============================================================
# 本地匹配
# ============================================================

def _query_terms(query: str) -> List[str]:
    """确定性拆词：去掉列举/图片意图词，保留词与中文 2-gram。"""
    text = str(query or "").casefold()
    # 先去掉完整的列举短语，避免“有哪些图片”留下“有哪些”被当成内容词。
    for cue in LIST_CUES:
        text = text.replace(cue.casefold(), " ")
    for cue in GENERIC_CUES:
        text = text.replace(cue.casefold(), " ")

    terms: List[str] = []
    seen = set()
    for raw in _WORD_SPLIT.split(text):
        token = raw.strip("._-")
        if len(token) < 2 or token in STOP_TERMS:
            continue
        candidates = [token]
        if _CJK_RE.search(token) and len(token) > 2:
            candidates.extend(token[index : index + 2] for index in range(len(token) - 1))
        for candidate in candidates:
            candidate = candidate.strip("._-")
            if len(candidate) < 2 or candidate in STOP_TERMS or candidate in seen:
                continue
            seen.add(candidate)
            terms.append(candidate)
            if len(terms) >= MAX_QUERY_TERMS:
                return terms
    return terms


def _is_listing(query: str) -> bool:
    text = str(query or "").strip().casefold()
    if not text:
        return False
    if text in {"图片", "照片", "截图", "图片资料", "image", "photo", "picture"}:
        return True
    return any(cue in text for cue in LIST_CUES)


def _score_record(record: Dict[str, Any], terms: Sequence[str]) -> Tuple[float, List[str]]:
    """关键词/描述/预览/文档名的重叠分；只用于本地排序，不跨通道比较。"""
    keywords = [str(item).casefold() for item in record.get("keywords") or [] if str(item)]
    caption = str(record.get("caption") or "").casefold()
    preview = str(record.get("ocr_preview") or "").casefold()
    name = _stem(str(record.get("doc_name") or "")).casefold()

    score = 0.0
    matched: List[str] = []
    for term in terms:
        best = 0.0
        for keyword in keywords:
            if not keyword:
                continue
            if term == keyword or term in keyword or (len(keyword) >= 2 and keyword in term):
                best = max(best, 3.0)
        if term in caption:
            best = max(best, 2.0)
        if term in name:
            best = max(best, 1.5)
        if term in preview:
            best = max(best, 1.0)
        if best:
            score += best
            matched.append(term)
    return score, matched


def to_evidence(
    record: Dict[str, Any],
    *,
    score: float = 0.0,
    text: str = "",
    channels: Sequence[str] = ("gallery",),
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Evidence:
    """把一条图片记录变成 `Evidence`；正文用预览或检索片段，描述留在 metadata。"""
    image_id = _text(record.get("image_id"))
    file_id = _text(record.get("file_id"))
    metadata: Dict[str, Any] = {
        "gallery": _gallery_meta(record),
        "channels": _strings(list(channels), limit=8),
    }
    if isinstance(extra_metadata, dict):
        metadata.update(extra_metadata)

    return Evidence(
        evidence_id=f"image:{image_id or file_id}",
        kind=IMAGE_KIND,
        source_id=image_id or file_id,
        doc_name=_text(record.get("doc_name")),
        file_id=file_id,
        image_id=image_id,
        text=str(text or record.get("ocr_preview") or ""),
        origin_url=_text(record.get("origin_url")),
        score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        metadata=metadata,
    )


def search_local(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    items: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Evidence], Dict[str, Any]]:
    """本地图片检索：返回证据与只含计数的统计。"""
    limit = _clamp_limit(limit)
    records = image_records(items)
    terms = _query_terms(query)
    listing = _is_listing(query)
    stats: Dict[str, Any] = {
        "total": len(records),
        "matched": 0,
        "returned": 0,
        "truncated": False,
        "terms_count": len(terms),
        "listing": listing,
        "reason": "ok",
    }

    if not records:
        stats["reason"] = "no_images"
        return [], stats
    if not terms and not listing:
        stats["reason"] = "no_terms"
        return [], stats

    scored: List[Tuple[float, Dict[str, Any], List[str]]] = []
    for record in records:
        if terms:
            score, matched = _score_record(record, terms)
            if score <= 0:
                continue
        else:
            score, matched = 0.0, []
        scored.append((score, record, matched))

    scored.sort(
        key=lambda entry: (
            -entry[0],
            str(entry[1].get("doc_name") or "").casefold(),
            str(entry[1].get("file_id") or ""),
        )
    )
    stats["matched"] = len(scored)
    selected = scored[:limit]
    stats["returned"] = len(selected)
    stats["truncated"] = len(scored) > len(selected)
    if not scored:
        stats["reason"] = "no_match"

    evidence = [
        to_evidence(
            record,
            score=score,
            channels=("gallery",),
            extra_metadata={"matched_terms": len(matched)},
        )
        for score, record, matched in selected
    ]
    return evidence, stats


# ============================================================
# 检索通道分类与配额合并
# ============================================================

def _merge_metadata(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(first)
    for key, value in second.items():
        if key == "gallery" and isinstance(value, dict):
            gallery_meta = dict(merged.get("gallery") or {})
            for gallery_key, gallery_value in value.items():
                if not gallery_meta.get(gallery_key):
                    gallery_meta[gallery_key] = gallery_value
            merged["gallery"] = gallery_meta
        elif key == "channels":
            merged[key] = _canonical_channels(_union(merged.get(key), value))
        elif key == "chunk_ids":
            merged[key] = _union(merged.get(key), value)
        elif key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    if "chunk_ids" in merged:
        merged["chunk_ids"] = list(merged["chunk_ids"])[:MAX_CHUNK_IDS]
    return merged


def _canonical_channels(values: Sequence[str]) -> List[str]:
    """通道顺序固定，避免本地/检索谁先到就谁在前。"""
    order = {"gallery": 0, "retrieval": 1}
    return sorted(
        _union(values, ()),
        key=lambda item: (order.get(item, 99), item),
    )


def _union(first: Any, second: Any) -> List[str]:
    found: List[str] = []
    seen = set()
    for source in (first, second):
        if not isinstance(source, (list, tuple)):
            continue
        for item in source:
            text = _text(item)
            if not text or text in seen:
                continue
            seen.add(text)
            found.append(text)
    return found


def _merge_pair(first: Evidence, second: Evidence) -> Evidence:
    """同一张图片的两条证据合并：正文取更长，分数取更高，通道/片段求并集。"""
    first_text = first.text if isinstance(first.text, str) else ""
    second_text = second.text if isinstance(second.text, str) else ""
    first_score = first.score if isinstance(first.score, (int, float)) else None
    second_score = second.score if isinstance(second.score, (int, float)) else None
    scores = [score for score in (first_score, second_score) if score is not None]

    return Evidence(
        evidence_id=first.evidence_id,
        kind=IMAGE_KIND,
        source_id=first.source_id or second.source_id,
        doc_name=first.doc_name or second.doc_name,
        file_id=first.file_id or second.file_id,
        image_id=first.image_id or second.image_id,
        text=second_text if len(second_text) > len(first_text) else first_text,
        origin_url=first.origin_url or second.origin_url,
        score=max(scores) if scores else None,
        metadata=_merge_metadata(
            first.metadata if isinstance(first.metadata, dict) else {},
            second.metadata if isinstance(second.metadata, dict) else {},
        ),
    )


def _aggregate_images(items: Optional[Iterable[Evidence]]) -> List[Evidence]:
    order: List[str] = []
    groups: Dict[str, Evidence] = {}
    try:
        source = list(items or [])
    except TypeError:
        return []
    for item in source:
        if not isinstance(item, Evidence) or item.kind != IMAGE_KIND:
            continue
        if item.evidence_id not in groups:
            groups[item.evidence_id] = item
            order.append(item.evidence_id)
        else:
            groups[item.evidence_id] = _merge_pair(groups[item.evidence_id], item)
    return [groups[evidence_id] for evidence_id in order]


def classify_evidence(
    items: Sequence[Any],
    *,
    image_map: Optional[Dict[str, Dict[str, Any]]] = None,
    gallery_items: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Any], List[Evidence]]:
    """把 `/query/data` 证据分成 (非图片, 图片)。

    - 只对 `chunk` / `reference` 按文档名分类；entity/relationship 不当作图片；
    - 同一张图片的多个 chunk 聚合为一条；
    - 非 `Evidence` 或坏字段原样归入非图片列表，不抛异常。
    """
    try:
        raw_items = list(items or [])
    except TypeError:
        return [], []
    if not raw_items:
        return [], []

    mapping = image_map if isinstance(image_map, dict) else image_map_from_items(gallery_items)
    others: List[Any] = []
    images: List[Evidence] = []

    for item in raw_items:
        if not isinstance(item, Evidence):
            others.append(item)
            continue
        if item.kind == IMAGE_KIND:
            images.append(item)
            continue
        if item.kind not in {"chunk", "reference"}:
            others.append(item)
            continue

        record = _lookup_record(mapping, item.doc_name) if item.doc_name else None
        if not record:
            others.append(item)
            continue

        metadata = dict(item.metadata) if isinstance(item.metadata, dict) else {}
        metadata["gallery"] = _gallery_meta(record)
        metadata["channels"] = ["retrieval"]
        if item.kind == "chunk" and item.source_id:
            metadata["chunk_ids"] = [item.source_id]

        image_id = _text(record.get("image_id"))
        file_id = _text(record.get("file_id"))
        images.append(
            Evidence(
                evidence_id=f"image:{image_id or file_id}",
                kind=IMAGE_KIND,
                source_id=image_id or file_id,
                doc_name=_text(record.get("doc_name")),
                file_id=file_id,
                image_id=image_id,
                text=item.text if isinstance(item.text, str) else "",
                origin_url=_text(record.get("origin_url")),
                score=item.score,
                metadata=metadata,
            )
        )

    return others, _aggregate_images(images)


def image_map_from_items(
    items: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """`classify_evidence` 的显式入口：从 injected items 或真实索引建映射。"""
    return image_map(items)


def _lookup_record(
    mapping: Dict[str, Dict[str, Any]], doc_name: str
) -> Optional[Dict[str, Any]]:
    """精确键优先，其次唯一的“去短 ID”别名。"""
    key = gallery.key_of(doc_name)
    if not key:
        return None
    record = mapping.get(key)
    if record is not None:
        return record
    loose = _loose_key(doc_name)
    return mapping.get(loose) if loose else None


def _sort_images(items: Sequence[Evidence]) -> List[Evidence]:
    return sorted(
        items,
        key=lambda item: (
            -(float(item.score) if isinstance(item.score, (int, float)) and not isinstance(item.score, bool) else 0.0),
            str(item.doc_name or "").casefold(),
            str(item.file_id or ""),
            str(item.evidence_id or ""),
        ),
    )


def merge_images(
    local: Sequence[Evidence],
    retrieval: Sequence[Evidence],
    *,
    limit: int = DEFAULT_LIMIT,
    local_quota: int = LOCAL_QUOTA,
    retrieval_quota: int = RETRIEVAL_QUOTA,
) -> List[Evidence]:
    """两通道合并：各自先取前 N 条轮转，余量再补位到 limit；按 image_id 去重。"""
    limit = _clamp_limit(limit)
    local_items = _sort_images(_aggregate_images(local))
    retrieval_items = _sort_images(_aggregate_images(retrieval))

    local_take = local_items[: max(0, int(local_quota))]
    retrieval_take = retrieval_items[: max(0, int(retrieval_quota))]

    selected: List[Evidence] = []
    index_by_id: Dict[str, int] = {}

    def add(item: Evidence) -> None:
        if item.evidence_id in index_by_id:
            position = index_by_id[item.evidence_id]
            selected[position] = _merge_pair(selected[position], item)
            return
        if len(selected) >= limit:
            return
        index_by_id[item.evidence_id] = len(selected)
        selected.append(item)

    for index in range(max(len(local_take), len(retrieval_take))):
        if index < len(local_take):
            add(local_take[index])
        if index < len(retrieval_take):
            add(retrieval_take[index])

    # 首轮配额之后用剩余候选补满：单通道可超过自己的首轮配额，总数不超过 limit。
    for item in local_items + retrieval_items:
        if len(selected) >= limit:
            break
        add(item)

    return selected[:limit]


__all__ = [
    "DEFAULT_LIMIT",
    "GENERIC_CUES",
    "IMAGE_KIND",
    "LIST_CUES",
    "LOCAL_QUOTA",
    "MAX_LIMIT",
    "RETRIEVAL_QUOTA",
    "classify_evidence",
    "image_map",
    "image_map_from_items",
    "image_records",
    "merge_images",
    "search_local",
    "to_evidence",
]
