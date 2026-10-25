"""P1B：证据 v1 —— 把一份作业档案收敛成可检索、可引用的最小证据。

为什么需要它：
    Context Builder（P1C）与前端引用都要回答同一组问题：「这份资料的标题/描述是什么、
    正文大概讲了什么、原图在哪」。这些信息现在散在 `result.json`（名字、描述、资源清单）
    与 `content.md`（正文、手写描述）里，调用方各拼一遍就会各错一遍。

本模块只做**派生**，不拥有任何数据：
    · 输入是作业档案（`result.json` 的字典）与正文（`content.md`），输出是纯字典；
    · 不写文件、不联网、不读 OCR 原始结果（识别文字就在 content.md 里）；
    · 永不抛异常：缺字段 / 缺文件 / 坏档案一律降级为空证据。

正文格式的约定（与 `ingest` 共用同一份常量，不在这里另写一份）：
    · 手写描述在 `ingest.CAPTION_MARK` 之后；
    · 内嵌图片 OCR 的结果以 `<!-- 以下文字由内嵌图片 OCR 得到 -->` 引入，属于正文；
    · 纯图片作业的整篇 OCR 文本就是正文（写入时没有额外标记）。

隐私与体积：
    `caption` 上限 2000 字、`ocr_preview` 上限 200 字、关键词 8 个、资源路径 20 条。
    索引里**不存整篇 OCR 正文**，正文仍在 `content.md`。
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

EVIDENCE_VERSION = 1
CAPTION_MAX_CHARS = 2000
PREVIEW_MAX_CHARS = 200
KEYWORDS_MAX = 8
ASSET_PATHS_MAX = 20
IMAGE_ROUTE = "image"

#: 关键词切分：空白/标点/符号都算分隔符（`\w` 包含中文与数字）
_KEYWORD_SPLIT = re.compile(r"[^\w]+", re.UNICODE)
#: HTML 注释（含 OCR / 描述的标记本身）：预览里不该出现
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SRC_RE = re.compile(r"""src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_SHORT_ID_RE = re.compile(r"_[0-9a-f]{6}$", re.IGNORECASE)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _ingest_module():
    """延迟取 ingest（ingest 顶层会导入 gallery → evidence，所以这里不能顶层导入它）。"""
    from app.services import ingest as ingest_service

    return ingest_service


def _caption_mark() -> str:
    try:
        return str(_ingest_module().CAPTION_MARK or "")
    except Exception:                                    # noqa: BLE001 - 证据构建不该因它失败
        return ""


def _read_content(file_id: str) -> str:
    try:
        path = _ingest_module().content_path(file_id)
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except Exception:                                    # noqa: BLE001 - 读不到就当没有正文
        return ""


def _plain(text: str) -> str:
    """去掉 HTML 注释/标签并压缩空白，得到可读的一行文本。"""
    value = _COMMENT_RE.sub(" ", str(text or ""))
    value = _TAG_RE.sub(" ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def split_caption(text: str):
    """把正文拆成 `(描述之外的正文, 手写描述)`（与 `ingest._report_caption_split` 同一规则）。"""
    body = str(text or "")
    mark = _caption_mark()
    if mark and mark in body:
        head, _, tail = body.partition(mark)
        return head.rstrip(), tail.strip()
    return body, ""


def preview(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    """正文的一行预览（不含标记/标签，长度有上限）。"""
    return _plain(text)[: max(0, int(limit))]


def keywords(*texts: str, limit: int = KEYWORDS_MAX) -> List[str]:
    """确定性的关键词：按分隔符切、长度 2–20、去重保序、数量有上限。

    不引入分词依赖：宁可不完美，也要每次运行结果一致、可测试。
    """
    limit = max(0, int(limit))
    if not limit:
        return []
    found: List[str] = []
    seen = set()
    for text in texts:
        for token in _KEYWORD_SPLIT.split(str(text or "")):
            token = token.strip("._-")
            if not (2 <= len(token) <= 20):
                continue
            key = token.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(token)
            if len(found) >= limit:
                return found
    return found


def _stem(name: str) -> str:
    """文件名/文档名 → 名字部分（去掉扩展名与末尾短 ID）。"""
    base = Path(str(name or "")).stem
    return _SHORT_ID_RE.sub("", base).strip(" ._-")


def _safe_relative(root: Path, value: str) -> str:
    """把正文里的相对引用收敛成 `root` 下的安全相对路径；不安全/不存在 → 空串。"""
    raw = str(value or "").strip().strip("\"'")
    if not raw or "://" in raw or raw.lower().startswith("data:"):
        return ""
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or _DRIVE_RE.match(normalized):
        return ""
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    relative = "/".join(parts)
    try:
        base = root.resolve()
        candidate = (root / Path(*parts)).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return ""
    return relative if candidate.is_file() else ""


def _source_relative(root: Path, data: Dict[str, Any]) -> str:
    """原件副本的作业目录相对路径（档案里存的是绝对路径）。"""
    raw = str(data.get("source_path") or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        base = root.resolve()
        candidate = (path if path.is_absolute() else root / path).resolve()
        relative = candidate.relative_to(base)
    except (OSError, ValueError):
        return ""
    return relative.as_posix() if candidate.is_file() else ""


def asset_paths(data: Dict[str, Any], content: str, *, file_id: str) -> List[str]:
    """作业目录内的真实资源路径：原件副本 + 落盘的内嵌图 + 正文里引用且存在的图。"""
    if not file_id:
        return []
    try:
        root = _ingest_module().job_dir(file_id)
    except Exception:                                    # noqa: BLE001 - 路径不可得就当作没有
        return []

    candidates: List[str] = [_source_relative(root, data)]
    assets = data.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if isinstance(asset, dict):
                candidates.append(str(asset.get("path") or ""))
    candidates.extend(_SRC_RE.findall(str(content or "")))

    found: List[str] = []
    seen = set()
    for raw in candidates:
        relative = _safe_relative(root, html.unescape(str(raw or "")))
        if not relative:
            continue
        key = relative.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(relative)
        if len(found) >= ASSET_PATHS_MAX:
            break
    return found


def from_job(
    data: Dict[str, Any],
    *,
    doc_name: str = "",
    content: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """把一份作业档案收敛成证据 v1；输入不可用时返回 None（绝不抛异常）。

    `content` 可注入（测试不必碰真实目录）；不传时按 `ingest.content_path(file_id)` 读取。
    """
    if not isinstance(data, dict):
        return None
    file_id = str(data.get("file_id") or "").strip()
    if not file_id:
        return None

    text = str(content or "") if content is not None else _read_content(file_id)
    head, tail = split_caption(text)
    caption = tail[:CAPTION_MAX_CHARS]
    if not caption:
        stored = data.get("caption")
        if isinstance(stored, dict):
            caption = str(stored.get("text") or "").strip()[:CAPTION_MAX_CHARS]

    is_image = str(data.get("route") or "") == IMAGE_ROUTE
    body = _plain(head) if is_image else ""
    name = str(doc_name or data.get("doc_name") or "")

    return {
        "version": EVIDENCE_VERSION,
        "kind": "image" if is_image else "document",
        "image_id": f"img:{file_id}:0" if is_image else "",
        "caption": caption,
        "ocr_preview": body[:PREVIEW_MAX_CHARS] if is_image else "",
        "ocr_chars": len(body) if is_image else 0,
        "asset_paths": asset_paths(data, text, file_id=file_id),
        "keywords": keywords(_stem(name), _stem(str(data.get("name") or "")), caption),
        "result_pointer": f"parsed/{file_id}/result.json",
        "content_pointer": f"parsed/{file_id}/content.md",
    }
