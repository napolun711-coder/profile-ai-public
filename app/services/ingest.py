"""入库编排（Phase 8）：解析 → 质量门 →（可选）OCR → 入库确认 → 入库。

**合规约束（唯一网关）**：只有本模块会调用 `clients.lightrag.upload()`。
因此"未通过质量检测的文件不会进知识库"这一承诺在代码层面只有一处需要审计。

设计要点：
1. 每个文件一个作业目录 `data/parsed/<file_id>/`，`result.json` 是**唯一的状态来源**；
2. 单线程工作队列：一次只处理一个文件（个人场景，避免并发外发/抢资源）；
3. 任何失败都不抛给 HTTP 层：状态写入 result.json（state=failed/blocked + detail）；
4. 送入 LightRAG 的一律是**统一 Markdown**，保证入库内容与质检内容完全一致；
5. Phase 15「入库确认门」：质检通过后**不再直接入库**，先停在 state=review 等用户
   在网页上点「入库 / 跳过」。是否走这道门由 `INGEST_CONFIRM` 决定：
   always（默认，每个文件都要确认一次）/ auto（建议为 ok 才自动放行）/ off（直接入库）。
   建议内容由 `services/advice.py` 生成（可解释的检查项 + 快速预览），只作提示，
   **永远不代替用户做决定**。
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.clients import lightrag
from app.core.config import get_settings
from app.core.errors import BODY_PREVIEW_LIMIT, UpstreamError, humanize_upstream
from app.core.paths import KNOWLEDGE_DIR, PARSED_DIR, safe_filename
from app.services import advice as advice_service
from app.services import evidence as evidence_service
from app.services import gallery as gallery_service
from app.services import ocr as ocr_service
from app.services import parse as parse_service
from app.services import quality as quality_service

logger = logging.getLogger(__name__)

# ---------- 状态常量 ----------
STATE_PARSING = "parsing"
STATE_CHECKING = "checking"
STATE_NEEDS_OCR = "needs_ocr"
STATE_OCR = "ocr"
STATE_INGESTING = "ingesting"
STATE_DONE = "done"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
# Phase 15：质检通过、等待用户确认入库（既不忙也未终结，占住作业目录）
STATE_REVIEW = "review"
# Phase 15：用户选择「跳过」（或等待超时归档），不会进知识库
STATE_SKIPPED = "skipped"
# Phase 16：曾经入库过，后来被用户在「文件管理」页从知识库删掉了（本机留档仍在）
STATE_REMOVED = "removed"

#: 每个状态的人话名字（提示语与时间线共用一处，避免同一件事两种说法）
STATE_LABELS = {
    STATE_PARSING: "正在本地解析",
    STATE_CHECKING: "正在质量校验",
    STATE_NEEDS_OCR: "需要 OCR 文字识别",
    STATE_OCR: "正在 OCR 识别",
    STATE_INGESTING: "正在提交知识库",
    STATE_DONE: "已入库",
    STATE_BLOCKED: "未通过质检",
    STATE_FAILED: "处理失败",
    STATE_REVIEW: "等你确认入库",
    STATE_SKIPPED: "已跳过",
    STATE_REMOVED: "已从知识库删除",
}

# ---------- Phase 15.2：处理过程时间线（「这一步在做什么、到哪了、花了多久」）----------
# 只用于展示：全部来自 result.json 的 timeline 段与既有字段，**不联网、不读盘、不参与任何判定**。
STEP_ACCEPTED = "accepted"
STEP_PARSING = "parsing"
STEP_CHECKING = "checking"
STEP_OCR = "ocr"
STEP_REVIEW = "review"
STEP_SUBMIT = "submitting"
STEP_INDEX = "indexing"
STEP_KEYS = (
    STEP_ACCEPTED,
    STEP_PARSING,
    STEP_CHECKING,
    STEP_OCR,
    STEP_REVIEW,
    STEP_SUBMIT,
    STEP_INDEX,
)
STEP_LABELS = {
    STEP_ACCEPTED: "已受理上传",
    STEP_PARSING: "本地解析",
    STEP_CHECKING: "质量校验",
    STEP_OCR: "OCR 文字识别",
    STEP_REVIEW: "等你确认入库",
    STEP_SUBMIT: "提交知识库",
    STEP_INDEX: "知识库解析与建索引",
}

BUSY_STATES = (STATE_PARSING, STATE_CHECKING, STATE_OCR, STATE_INGESTING)
# review / skipped 也是「停下来等用户」的终态（不算忙，但轮询可以停）；removed 同属终态
TERMINAL_STATES = (
    STATE_DONE,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_REVIEW,
    STATE_SKIPPED,
    STATE_REMOVED,
)
# 允许「重试」的状态：done 不在其中（重复处理会产生重复文档，Phase 9 起禁止）；
# review / skipped 也不在其中 —— 等确认的作业该点「入库 / 跳过」，
# 跳过的作业该点「仍要入库」（不必重解析），都不需要再来一遍解析流水线。
RETRYABLE_STATES = (STATE_BLOCKED, STATE_FAILED, STATE_NEEDS_OCR)

ACTION_PARSE = "parse"
ACTION_OCR = "ocr"
ACTION_FORCE = "force"
ACTION_RETRY = "retry"
# Phase 15：入库确认门（质检通过后的两个选择）
ACTION_INGEST = "ingest"
ACTION_SKIP = "skip"
ACTIONS = (ACTION_OCR, ACTION_FORCE, ACTION_RETRY, ACTION_INGEST, ACTION_SKIP)

MARKDOWN_NAME = "content.md"
SOURCE_PREFIX = "source"
RESULT_NAME = "result.json"
ASSETS_DIR = "assets"
#: Phase 15.2 编辑正文：编辑前的上一版留在这里（只留一份，够看「改之前长什么样」）
BACKUP_NAME = "content.before-edit.md"
#: Phase 15.2 编辑正文的长度上限（与解析上限 PARSE_MAX_CHARS 同量级：解析出来的正文本来就 ≤ 这个数）
CONTENT_MAX_CHARS = 2_000_000
#: result.json 原子替换的重试次数（Windows 上偶发的瞬时占用，见 _write_result 注释）
RESULT_WRITE_RETRIES = 5

# ---------- Phase 17：图片资料的命名与手写描述 ----------
#: 用户输入的名字最多这么多字（短 ID 与扩展名另算）：再长也没人看得完，还会撑破页面
DOC_NAME_MAX_CHARS = 50
#: 文档名里的短 ID 长度（内容指纹 sha256 的前 6 位：稳定、可复现、够区分）
DOC_ID_CHARS = 6
#: 手写描述在正文里的分隔标记（与内嵌图片 OCR 的分隔标记同一风格：一眼看出哪段是人写的）
CAPTION_MARK = "<!-- 以下文字由我手动描述 -->"
#: 定名时剥掉的后缀（页面回填的是完整名 `xxx_3fcb99.md`，用户也可能连原文件名一起粘进来）；
#: 只剥这几个，别的点号一律算名字的一部分 —— 否则「v1.2 报告」会被截成「v1」
DOC_NAME_SUFFIXES = (
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
)
#: 文档名末尾的短 ID（`_3fcb99`）：用户改不了它，所以定名与回填都得认出来并剥掉
SHORT_ID_PATTERN = re.compile(r"_[0-9a-f]{6}$", re.IGNORECASE)

# 图片 OCR 的两种范围：整篇（PDF/图片）与文档内嵌图片
OCR_SCOPE_DOCUMENT = "document"
OCR_SCOPE_IMAGES = "images"

# 闸门不变量触发时的用户可见原因（属于内置自检，正常流程不该出现）
GATE_INVARIANT_DETAIL = "内部错误：检测到未经质检的内容试图入库，已阻止（未提交知识库）"
GATE_FORCED_NOTE = "（已按你的要求强制入库）"
GATE_CONFIRMED_NOTE = "（已按你的确认入库）"

# Phase 15：确认门相关的固定文案（前端也按这些说法展示，改文案只改这一处）
DETAIL_SKIPPED = "已跳过：本文件没有进入知识库；如需入库可点「仍要入库」"
DETAIL_REVIEW_FALLBACK = "已通过质量校验，等你确认后才会进知识库"
DETAIL_EXPIRED = "等待确认超过 {hours} 小时，已自动跳过（未入库）；如需入库请重新上传"
# Phase 16：用户在「文件管理」页从知识库删掉了这条（本机留档与原件都还在）
DETAIL_REMOVED_FROM_KB = "已按你的选择从知识库删除（本机留档与原件都还在；要重新入库可点「仍要入库」）"

#: 确认门的合法取值（与 core/config.INGEST_CONFIRM_MODES 一致）
CONFIRM_ALWAYS = "always"
CONFIRM_AUTO = "auto"
CONFIRM_OFF = "off"

#: Phase 16：作业来源（页面显示 / 决定是否给确认门做按作业覆盖）
JOB_ORIGINS = ("upload", "inbox")

_state_lock = threading.Lock()      # 保护 result.json 的读改写
_busy_lock = threading.Lock()       # 保护 _active_jobs 计数
_session_lock = threading.Lock()    # Phase 15：保护「本次运行内全部允许」开关
_pending_lock = threading.Lock()    # Phase 15.1：保护「同一作业同一动作只排一次」的登记表
_active_jobs = 0                    # 排队 + 处理中的作业数（Phase 10：停服前判断忙不忙）
#: Phase 15.1 幂等：已经在队列里 / 正在执行的 (file_id, action)——重复点击直接丢弃。
#: 存在内存里：进程重启后队列本来也空了，不需要持久化。
_pending_keys: set = set()
# Phase 15：「本次运行内全部允许」——打开后新文件不再停下确认（进程重启即复位）。
# 只放在内存里，**不落盘**：用户偶尔想批量导入时才用，不该变成永久设置（永久设置在设置页）。
_session_allow_all = False
_queue: "queue.Queue[Tuple[str, str]]" = queue.Queue()
_worker: Optional[threading.Thread] = None


# ============================================================
# 路径与读写
# ============================================================

def job_dir(file_id: str) -> Path:
    return PARSED_DIR / safe_filename(file_id)


def result_path(file_id: str) -> Path:
    return job_dir(file_id) / RESULT_NAME


def content_path(file_id: str) -> Path:
    return job_dir(file_id) / MARKDOWN_NAME


def source_path(file_id: str, ext: str = "") -> Path:
    return job_dir(file_id) / f"{SOURCE_PREFIX}{ext}"


def assets_dir(file_id: str) -> Path:
    """内嵌图片落盘目录：data/parsed/<file_id>/assets/。"""
    return job_dir(file_id) / ASSETS_DIR


def read_result(file_id: str) -> Optional[Dict[str, Any]]:
    """读取作业档案；不存在或损坏时返回 None。"""
    path = result_path(file_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("result.json 损坏或不可读：%s", path)
        return None


def _write_result(file_id: str, data: Dict[str, Any]) -> None:
    """原子写 result.json：临时文件 + replace。

    Windows 上 `replace` 偶尔会撞上 `PermissionError`（杀毒 / 索引器 / 并发读取
    的瞬间句柄），教训是：这会直接把一个本来快成功的作业判成 `failed`
    （真机出现过「拒绝访问：result.json.tmp -> result.json」）。
    所以这里**先重试几次**，仍不行就退化成直接覆盖写（宁可短暂非原子，
    也不能因为一次瞬时占用丢掉整个作业）。
    """
    directory = job_dir(file_id)
    directory.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now()
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    target = result_path(file_id)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(payload, encoding="utf-8")

    for attempt in range(RESULT_WRITE_RETRIES):
        try:
            temp.replace(target)
            return
        except OSError as exc:
            if attempt == RESULT_WRITE_RETRIES - 1:
                logger.warning(
                    "result.json 原子替换失败（第 %d 次）：%s：%s，改为直接覆盖写",
                    attempt + 1,
                    file_id,
                    exc,
                )
                break
            time.sleep(0.05 * (attempt + 1))

    try:
        target.write_text(payload, encoding="utf-8")
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:  # 临时文件删不掉不影响结果
            pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _update(file_id: str, **changes: Any) -> Dict[str, Any]:
    """更新顶层状态字段（线程安全）并返回最新档案。"""
    with _state_lock:
        data = read_result(file_id) or {"file_id": file_id, "created_at": _now()}
        data.update(changes)
        _write_result(file_id, data)
        return dict(data)


def _patch_section(file_id: str, section: str, **changes: Any) -> Dict[str, Any]:
    """更新 result.json 中某个子对象（quality / ocr / lightrag / advice / edit）。"""
    with _state_lock:
        data = read_result(file_id) or {"file_id": file_id, "created_at": _now()}
        block = dict(data.get(section) or {})
        block.update(changes)
        data[section] = block
        _write_result(file_id, data)
        return dict(data)


def _touch_step(file_id: str, key: str) -> None:
    """记下某个阶段的开始时间（Phase 15.2 时间线）。

    同一阶段重新开始时**覆盖**旧时间：时间线描述的是「这一轮」的过程，
    重试 / 重新质检 / 再次提交都该显示最新一次的时间，而不是第一次的。
    纯记录，不影响任何判定；写失败也不抛（时间线只是展示）。
    """
    try:
        with _state_lock:
            data = read_result(file_id) or {"file_id": file_id, "created_at": _now()}
            timeline = dict(data.get("timeline") or {})
            timeline[key] = _now()
            data["timeline"] = timeline
            _write_result(file_id, data)
    except Exception as exc:  # noqa: BLE001 - 时间线写不进去也不该影响入库流程
        logger.debug("记录处理步骤失败（忽略）：%s / %s：%s", file_id, key, exc)


# ============================================================
# 作业创建与派发
# ============================================================

def make_file_id(digest: str) -> str:
    """file_id = 本地时间 + 内容指纹前 8 位（可读且可定位）。"""
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{digest[:8]}"


# ---------- Phase 17：图片资料的命名（`<用户输入的名字>_<短ID>.md`）----------
#
# 为什么图片要单独起名：图片没有文字，进知识库的其实是**用户手写的一段描述**，
# 而描述跟原文件名（`IMG_20261016.jpg`）往往毫无关系 —— 让用户自己起个
# 「书桌照片」这样的名字，提问时看到的引用才有意义。
#
# 名字的构成：
#     <用户输入的名字>_<短ID>.md
#     · 短ID = 内容 sha256 的前 6 位（同一张图永远得到同一个 ID，与 file_id 后半段同源）；
#     · 用户只改前面的名字部分，短 ID 由服务端补 —— 「图片 ↔ 文档」的锚点不能由用户改写，
#       否则双击打开原图、按行删除都得靠猜；
#     · 名字 ≤ DOC_NAME_MAX_CHARS 字，非法字符按上传留档的同一套规则替换成 _。

def doc_id_of(data: Dict[str, Any]) -> str:
    """这份资料的短 ID（内容指纹前 6 位；档案里没有指纹时退回 file_id 的尾部）。"""
    digest = str(data.get("sha256") or "").strip().lower()
    if not digest:
        digest = str(data.get("file_id") or "").split("-")[-1]
    return safe_filename(digest).lower()[:DOC_ID_CHARS] or "000000"


def is_image_job(data: Dict[str, Any]) -> bool:
    """是不是「纯图片」资料（路由 = image）。

    用途：这类作业解析不出文字是**正常现象**（不是失败），所以
    · 允许直接写描述（见 _edit_reason）；
    · 复检时按纯文本判质（见 edit_content 里的 route_for_check）；
    · 入库时给一个带短 ID 的默认名（见 doc_name_for）。
    """
    return str(data.get("route") or "") == "image"


def _clean_doc_stem(raw: str) -> str:
    """把「名字部分」收敛成安全的一段：非法字符 → `_`、连续下划线压成一个、首尾的点/空格/下划线去掉。

    与上传留档的 `safe_filename` 同一套规则，唯一区别：**不把 `/` 当目录分隔符** ——
    用户输入的是一个名字，写「书桌/照片」应当得到「书桌_照片」，而不是只剩「照片」。
    """
    kept = [char if (char.isalnum() or char in "._-") else "_" for char in str(raw or "")]
    cleaned = "".join(kept)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip(" ._-")


def _strip_doc_name_noise(raw: str) -> str:
    """剥掉名字里我们自己的包装：认得的扩展名 + 末尾短 ID。

    页面回填的是**完整文档名**（`书桌照片_3fcb99.md`），用户不动它直接保存时，
    这里必须把包装剥干净，否则会长出 `书桌照片_3fcb99_3fcb99.md` 这种名字。
    """
    text = _clean_doc_stem(raw)
    suffix = Path(text).suffix.lower()
    if suffix in DOC_NAME_SUFFIXES:                      # 只剥认得的后缀，「v1.2 报告」不受影响
        text = text[: -len(suffix)].strip(" ._-")
    return SHORT_ID_PATTERN.sub("", text).strip(" ._-")


def default_doc_name_stem(data: Dict[str, Any]) -> str:
    """图片资料的默认名字部分（原文件名主干，清洗限长）。"""
    stem = _clean_doc_stem(Path(str(data.get("name") or "document")).stem)
    return (stem or "document")[:DOC_NAME_MAX_CHARS].strip(" ._-") or "document"


def suggested_doc_name(data: Dict[str, Any]) -> str:
    """图片资料的默认完整文档名：`<原文件名主干>_<短ID>.md`（前端据此预填）。"""
    return f"{default_doc_name_stem(data)}_{doc_id_of(data)}.md"


def make_doc_name(raw: str, data: Dict[str, Any]) -> str:
    """用户输入的名字 → 最终文档名（**唯一命名出口**，Phase 17）。

    幂等：用户提交里已经带着扩展名或 `_<短ID>` 时不会重复追加（页面回填的往往是完整名）。
    """
    doc_id = doc_id_of(data)
    cleaned = _strip_doc_name_noise(raw)[:DOC_NAME_MAX_CHARS].strip(" ._-")
    if not cleaned:
        cleaned = default_doc_name_stem(data)
    return f"{cleaned}_{doc_id}.md"


def doc_name_stem_of(data: Dict[str, Any]) -> str:
    """这份资料当前文档名的**名字部分**（去掉 `_<短ID>` 与扩展名；前端名字输入框的回填值）。"""
    explicit = str(data.get("doc_name") or "").strip()
    if not explicit:
        return default_doc_name_stem(data)
    return _strip_doc_name_noise(explicit) or default_doc_name_stem(data)


def doc_name_for(data: Dict[str, Any]) -> str:
    """这份作业**当前**在知识库里的文档名（_stage_ingest 与 library 都读它，规则只有这一份）。

    · 档案里已经定过名（`doc_name`，用户在「写描述」时定的，或上次入库时落的）→ 用它；
    · 纯图片资料还没定名 → 给带短 ID 的默认名（用户不改也能入库）；
    · 其余资料 → 老规则 `<原文件名主干>.md`，**历史文档的映射因此一点不变**。
    """
    explicit = str(data.get("doc_name") or "").strip()
    if explicit:
        return explicit
    if is_image_job(data):
        return suggested_doc_name(data)
    return f"{safe_filename(Path(str(data.get('name') or 'document')).stem) or 'document'}.md"


def doc_name_conflict(markdown_name: str, data: Dict[str, Any]) -> str:
    """这个名字能不能用？返回空串 = 能用，否则是「为什么不能用」的人话。

    上游**允许同名文档**（真机实测知识库里有 3 份同名 `图文测试.md`），所以「不撞名」
    只能我们自己保证 —— 撞了就停下来说清楚，让用户改个名字或先删掉旧的：

    1. 本机索引（`data\\gallery\\index.json`）里这个名字属于**别的作业** → 拦下；
       若属于本作业（例如删除后重新入库）→ 放行；若两边的 sha256 相同（同一张图重复
       上传）→ 放行（内容一样，多一份同名文档不会让人看错）；
    2. 索引里查不到 → 问一次知识库（`documents_by_name`），真有同名文档（别的工具传的）
       也拦下。**问不到就放行**：不能让一次网络故障把正常入库堵死，更不能谎报「名字被占用」。
    """
    name = str(markdown_name or "").strip()
    if not name:
        return ""

    owner = str(data.get("file_id") or "")
    digest = str(data.get("sha256") or "")
    try:
        hit = gallery_service.lookup(name)
    except Exception as exc:                             # noqa: BLE001 - 索引读不到按无占用处理
        logger.debug("读取图片资料索引失败（按无占用处理）：%s", exc)
        hit = None

    if hit:
        other = str(hit.get("file_id") or "")
        if not other or other == owner:
            return ""                                    # 就是本作业自己
        if digest and str(hit.get("sha256") or "") == digest:
            logger.info("同名文档但内容相同（同一份资料重复上传），放行：%s", name)
            return ""
        return (
            f"「{name}」这个名字已经给了另一份资料（作业 {other}）："
            "请改个名字再入库，或先到「文件管理」页把旧的那份删掉"
        )

    try:
        found = lightrag.documents_by_name(name)
    except Exception as exc:                             # noqa: BLE001 - 查不到不拦
        logger.debug("按文档名查知识库失败（按无占用处理）：%s", exc)
        return ""

    if found:
        doc_id = str((found[0] or {}).get("id") or "")
        return (
            f"知识库里已经有一份叫「{name}」的文档（{doc_id}）："
            "换一个名字就能入库；确实想用这个名字，请先到「文件管理」页删掉旧的那份"
        )
    return ""


def _append_caption(file_id: str, addition: str) -> str:
    """把手写描述并入正文：**保留已有正文**（图片的 OCR 文本），描述部分可反复修改。

    描述块由 `CAPTION_MARK` 标记；再次保存描述时先删掉上一段描述块再加新的
    ——「描述追加」不等于「描述越堆越多」，否则连点两次保存就出现两段一样的描述。
    """
    current = _report_caption_split(_read_content(file_id))[0]
    mark = CAPTION_MARK
    addition = str(addition or "").strip()
    merged = f"{current.rstrip()}\n\n{mark}\n\n{addition}".strip() if current.strip() else f"{mark}\n\n{addition}"
    _write_content(file_id, merged)
    return merged


def _report_caption_split(text: str) -> Tuple[str, str]:
    """把正文拆成 `(描述之外的正文, 上次写的描述)`（没有描述块时后半段是空串）。"""
    body = str(text or "")
    if CAPTION_MARK not in body:
        return body, ""
    index = body.index(CAPTION_MARK)
    head = body[:index].rstrip()
    tail = body[index + len(CAPTION_MARK):].strip()
    return head, tail


def create_job(
    *,
    filename: str,
    content: bytes,
    content_type: str = "",
    archived_path: str = "",
    origin: str = "upload",
    confirm: str = "",
) -> Dict[str, Any]:
    """建立作业：落盘原件副本 + 初始化 result.json（state=parsing）。

    Phase 16 新增两个可选参数（老调用方不用改）：
    · `origin`：作业来源（"upload" 网页上传 / "inbox" 文件夹轮询），只影响展示与统计；
    · `confirm`：**按作业**覆盖确认门（"always" / "auto" / "off"，留空 = 用全局设置）。
      文件夹轮询用它实现 `INBOX_CONFIRM`（默认 auto）：其它入口的确认门行为一点不变。
    """
    digest = hashlib.sha256(content).hexdigest()
    file_id = make_file_id(digest)
    suffix = Path(filename).suffix.lower()
    route = parse_service.detect_route(filename)

    directory = job_dir(file_id)
    directory.mkdir(parents=True, exist_ok=True)
    source = source_path(file_id, suffix)
    source.write_bytes(content)

    data: Dict[str, Any] = {
        "file_id": file_id,
        "name": filename,
        "ext": suffix,
        "route": route,
        "route_label": parse_service.ROUTE_LABELS.get(route, route),
        "size": len(content),
        "sha256": digest,
        "content_type": content_type or "",
        "source_path": str(source),
        "archived_path": archived_path,
        "pages": 0,
        "images": 0,
        "parser": "",
        "assets": [],
        "quality": {
            "level": "",
            "score": 0,
            "reasons": [],
            "metrics": {},
            "forced": False,
            "forced_at": "",
            "forced_reasons": [],
        },
        "ocr": {
            "used": False,
            "provider": "",
            "model": "",
            "job_id": "",
            "scope": "",
            "pages": 0,
            "total_pages": 0,
            "images": 0,
            "recognized_images": 0,
            "elapsed_s": 0.0,
            "error": None,
            "triggered_by": "",
        },
        "lightrag": {"state": "queued", "track_id": "", "error": None},
        # Phase 16：来源（upload / inbox）与确认门的按作业覆盖（见 _stage_confirm）
        "origin": origin if origin in JOB_ORIGINS else "upload",
        "confirm_override": (
            confirm if confirm in (CONFIRM_ALWAYS, CONFIRM_AUTO, CONFIRM_OFF) else ""
        ),
        # Phase 15：入库建议（质检通过后由 services/advice.py 写入；只作提示）
        "advice": {},
        # Phase 17：知识库里的文档名（图片资料由用户在「写描述」时定名，见 doc_name_for）
        "doc_name": "",
        # Phase 17：人工写的描述（图片资料的主要入库内容；描述块由 CAPTION_MARK 标记）
        "manual_text": False,
        "manual_text_at": "",
        "caption": {"count": 0, "chars": 0, "at": "", "text": ""},
        "state": STATE_PARSING,
        "detail": "已受理，正在本地解析…",
        # Phase 15：进入确认门的时间（用于等待超时归档）与跳过的原因
        "review_since": "",
        "skipped_at": "",
        # Phase 15.2：处理过程时间线（每步开始时间）+ 编辑正文的留痕
        "timeline": {STEP_ACCEPTED: _now()},
        "edit": {"count": 0, "edited_at": "", "before_chars": 0, "after_chars": 0},
        "created_at": _now(),
        "updated_at": _now(),
    }

    with _state_lock:
        _write_result(file_id, data)

    logger.info("新建入库作业：%s（%s，%d 字节）", file_id, filename, len(content))
    return data


def enqueue(file_id: str, action: str = ACTION_PARSE) -> bool:
    """把作业放进工作队列（后台线程单并发处理）。

    Phase 15.1：同一作业的同一动作**在队列里只允许存在一份**。
    用户在确认门上连点「入库 / 跳过」时页面会发好几次请求，以前每一次都会真的入库一次
    （logs/app.log 里能看到 2 秒内 11 次动作、5 次被知识库 409 拒绝），
    现在重复提交直接丢弃并返回 False，调用方据此回一句「这次点击已忽略」。
    """
    key = (file_id, action)
    with _pending_lock:
        if key in _pending_keys:
            logger.info("忽略重复提交：%s（action=%s 已在队列/处理中）", file_id, action)
            return False
        _pending_keys.add(key)

    try:
        _ensure_worker()
        _queue.put(key)
    except Exception:
        with _pending_lock:
            _pending_keys.discard(key)
        raise
    return True


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_worker_loop, name="ingest-worker", daemon=True)
        _worker.start()
        logger.debug("入库工作线程已启动")


def is_busy() -> bool:
    """是否有作业在排队或处理中（Phase 10：网页停服前据此决定要不要延后）。

    网页关闭时，如果有文件正在解析 / 入库，就把倒计时从 25 秒延长到 5 分钟，
    避免用户刚上传完资料、索引做到一半就被停掉。
    """
    with _busy_lock:
        if _active_jobs > 0:
            return True
    return not _queue.empty()


def _worker_loop() -> None:
    global _active_jobs
    while True:
        file_id, action = _queue.get()
        with _busy_lock:
            _active_jobs += 1
        try:
            run_job(file_id, action)
        except Exception:  # noqa: BLE001 - 后台线程必须兜底，否则线程会死掉
            logger.exception("入库作业异常：%s（action=%s）", file_id, action)
            _update(file_id, state=STATE_FAILED, detail="内部错误，请查看 logs/app.log")
        finally:
            with _busy_lock:
                _active_jobs -= 1
            with _pending_lock:
                _pending_keys.discard((file_id, action))
            _queue.task_done()


# ============================================================
# 各阶段：解析 → 质检 →（OCR）→ 入库
# ============================================================

def run_job(file_id: str, action: str = ACTION_PARSE) -> Dict[str, Any]:
    """同步执行一次作业（后台线程与单测都走这一个入口）。"""
    settings = get_settings()
    if not read_result(file_id):
        logger.warning("作业不存在：%s", file_id)
        return {"file_id": file_id, "state": STATE_FAILED, "detail": "作业不存在"}

    try:
        if action in (ACTION_INGEST, ACTION_SKIP):
            # Phase 15.1 幂等：用户连点时可能有多个作业排在同一动作上，
            # 执行前再核对一次状态 —— 已经不在「等确认」就整段忽略，绝不重复调 LightRAG。
            current = str((read_result(file_id) or {}).get("state") or "")
            if current != STATE_REVIEW:
                logger.info(
                    "忽略已处理过的确认动作：%s（action=%s，当前状态=%s）",
                    file_id,
                    action,
                    current,
                )
                return read_result(file_id) or {
                    "file_id": file_id,
                    "state": current,
                    "detail": "该操作已经处理过（本次忽略）",
                }
        if action == ACTION_OCR:
            data = read_result(file_id) or {}
            scope = str((data.get("ocr") or {}).get("scope") or "")
            if scope == OCR_SCOPE_IMAGES:
                return _stage_ocr_images(file_id, settings, triggered_by="user")
            return _stage_ocr(file_id, settings, triggered_by="user")
        if action == ACTION_FORCE:
            return _stage_ingest(file_id, settings, forced=True)
        if action == ACTION_INGEST:
            # Phase 15：用户在确认门上点了「入库」（质检已通过，按正常路径入库）
            return _stage_ingest(file_id, settings, forced=False, confirmed=True)
        if action == ACTION_SKIP:
            # Phase 15：用户在确认门上点了「跳过」
            return _stage_skip(file_id)
        return _stage_pipeline(file_id, settings, allow_auto_ocr=True)
    except UpstreamError as exc:
        logger.error("入库失败（上游）：%s | %s", file_id, exc.message)
        _patch_section(file_id, "lightrag", state="failed", error=exc.message)
        return _update(file_id, state=STATE_FAILED, detail=exc.message)
    except Exception as exc:  # noqa: BLE001 - 失败一律写进档案，不抛给调用方
        logger.exception("作业执行异常：%s", file_id)
        return _update(
            file_id, state=STATE_FAILED, detail=f"内部错误（{type(exc).__name__}）：{exc}"
        )


def _failed_at_ingest(data: Dict[str, Any]) -> bool:
    """是不是「解析与质检都过了、只是提交知识库那一步被拒」（Phase 15.1，决定重试从哪继续）。"""
    level = str((data.get("quality") or {}).get("level") or "")
    lightrag_state = str((data.get("lightrag") or {}).get("state") or "")
    return level == quality_service.LEVEL_PASS and lightrag_state == "failed"


def _stage_pipeline(file_id: str, settings, *, allow_auto_ocr: bool) -> Dict[str, Any]:
    """首次处理（或重试）：已有 OCR 文本就直接复检，否则重新解析。"""
    data = read_result(file_id) or {}
    ocr_block = data.get("ocr") or {}
    text = _read_content(file_id)

    # Phase 15.1：解析与质检都已经过了，只是「提交知识库」被拒绝（例如重复上传）——
    # 重试必须从确认门继续，不能重跑一遍解析：否则作业又被送回确认界面，
    # 用户会看到「点了确认 → 又出现确认」，以为程序坏了（logs/app.log 00:52 就是这个现象）。
    if text and _failed_at_ingest(data):
        logger.info("重试：解析与质检已通过，直接从确认门继续（不重新解析）：%s", file_id)
        return _stage_quality(
            file_id,
            settings,
            text,
            route=str(data.get("route") or "text"),
            pages=int(data.get("pages") or 1),
            allow_auto_ocr=False,
            images_settled=True,
        )

    if ocr_block.get("used") and text:
        return _stage_quality(
            file_id,
            settings,
            text,
            route="text",
            pages=int(data.get("pages") or 1),
            allow_auto_ocr=False,
            images_settled=True,
        )

    return _stage_parse(file_id, settings, allow_auto_ocr=allow_auto_ocr)


def _stage_parse(file_id: str, settings, *, allow_auto_ocr: bool) -> Dict[str, Any]:
    data = read_result(file_id) or {}
    _update(file_id, state=STATE_PARSING, detail="正在本地解析…")
    _touch_step(file_id, STEP_PARSING)

    source = _source_file(data)
    if source is None or not source.is_file():
        return _update(
            file_id,
            state=STATE_FAILED,
            detail="找不到上传的原件副本（data/parsed 目录被清理过？），请重新上传",
        )

    result = parse_service.parse_file(source, data.get("name") or source.name)
    stored_assets = _store_assets(file_id, result.assets)
    _update(
        file_id,
        route=result.route,
        route_label=result.route_label,
        pages=result.pages,
        images=len(stored_assets) or result.images,
        parser=result.parser,
        assets=stored_assets,
    )

    if not result.ok:
        logger.warning("本地解析失败：%s → %s", file_id, result.detail)
        _patch_section(
            file_id,
            "quality",
            level=quality_service.LEVEL_BLOCKED,
            score=0,
            reasons=[result.detail],
            metrics={},
        )
        _patch_section(file_id, "lightrag", state="skipped", error=result.detail)
        return _update(file_id, state=STATE_BLOCKED, detail=result.detail)

    _write_content(file_id, result.text)
    _update(
        file_id,
        detail=f"本地解析完成（{result.route_label}）：{result.char_total} 个有效字符",
    )
    return _stage_quality(
        file_id,
        settings,
        result.text,
        route=result.route,
        pages=result.pages,
        allow_auto_ocr=allow_auto_ocr,
    )


def _stage_quality(
    file_id: str,
    settings,
    text: str,
    *,
    route: str,
    pages: int,
    allow_auto_ocr: bool,
    images_settled: bool = False,
) -> Dict[str, Any]:
    _update(file_id, state=STATE_CHECKING, detail="正在进行质量检测…")
    _touch_step(file_id, STEP_CHECKING)
    report = quality_service.evaluate(
        text, route=route, pages=pages, routes=quality_service.ocr_routes(settings)
    )
    _patch_section(
        file_id,
        "quality",
        level=report.level,
        score=report.score,
        reasons=report.reasons,
        metrics=report.metrics,
    )
    logger.info("质量检测：%s → %s", file_id, quality_service.describe(report))

    if report.level == quality_service.LEVEL_PASS:
        stop_reason = "" if images_settled else _image_stop_reason(settings, read_result(file_id) or {})
        if stop_reason:
            if allow_auto_ocr and settings.ocr_mode == "auto" and settings.ocr_enabled:
                return _stage_ocr_images(file_id, settings, triggered_by="auto")
            _patch_section(file_id, "ocr", scope=OCR_SCOPE_IMAGES)
            return _update(file_id, state=STATE_NEEDS_OCR, detail=stop_reason)
        # Phase 15：质检通过后先过「入库确认门」，是否直接入库由 INGEST_CONFIRM 决定
        return _stage_confirm(file_id, settings, text)

    if report.level == quality_service.LEVEL_NEEDS_OCR:
        _patch_section(file_id, "ocr", scope=OCR_SCOPE_DOCUMENT)
        if allow_auto_ocr and settings.ocr_mode == "auto" and settings.ocr_enabled:
            return _stage_ocr(file_id, settings, triggered_by="auto")
        return _update(file_id, state=STATE_NEEDS_OCR, detail=report.reasons[0])

    _patch_section(
        file_id, "lightrag", state="skipped", error="未通过质量检测，未提交知识库"
    )
    return _update(file_id, state=STATE_BLOCKED, detail=report.reasons[0])


# ============================================================
# Phase 15：入库确认门（质检通过 → 等用户确认 → 入库 / 跳过）
# ============================================================

def _confirm_mode(settings) -> str:
    """当前生效的确认门取值（非法值一律回落到默认 always，宁可信其有）。"""
    mode = str(getattr(settings, "ingest_confirm", CONFIRM_ALWAYS) or "").strip().lower()
    return mode if mode in (CONFIRM_ALWAYS, CONFIRM_AUTO, CONFIRM_OFF) else CONFIRM_ALWAYS


def _stage_confirm(file_id: str, settings, text: str) -> Dict[str, Any]:
    """质检通过后的岔路口：按设置直接入库，或停下等用户确认。

    三种走法：
    · off    → 不问，直接入库（用户明确关掉了这道门）；
    · auto   → 只有建议结论为 ok（没有值得提的点）才直接入库，否则照样停下；
    · always → 一律停下等用户点「入库 / 跳过」（默认，每个文件都问一次）。
    会话级「全部允许」（网页上的开关）优先于上面三种：打开后本进程内不再停。
    """
    data = read_result(file_id) or {}
    assets = list(data.get("assets") or [])

    result = advice_service.advise(
        text,
        quality=data.get("quality") or {},
        route=str(data.get("route") or "text"),
        route_label=str(data.get("route_label") or ""),
        pages=int(data.get("pages") or 1),
        file_name=str(data.get("name") or ""),
        images_total=len(assets),
        images_pending=_pending_images(data),
        ocr_used=bool((data.get("ocr") or {}).get("used")),
        min_chars=int(getattr(settings, "quality_min_chars", 50) or 0),
        max_unocr_images=int(getattr(settings, "quality_max_unocr_images", 0) or 0),
    )

    mode = _confirm_mode(settings)
    # Phase 16：按作业覆盖（文件夹轮询 INBOX_CONFIRM，默认 auto）——
    # 只覆盖这一个作业，全局设置与其它入口的行为完全不变。
    override = str(data.get("confirm_override") or "").strip().lower()
    if override in (CONFIRM_ALWAYS, CONFIRM_AUTO, CONFIRM_OFF):
        mode = override
    forced = session_allow_all()
    auto = mode == CONFIRM_OFF or forced or (mode == CONFIRM_AUTO and result.auto_ok)
    allowed_by = CONFIRM_OFF if mode == CONFIRM_OFF else ("session" if forced else ("auto" if auto else ""))

    # 建议原文写进档案：用户点开作业、或事后复查时都看得到「当时为什么这么建议」
    _patch_section(file_id, "advice", **result.to_dict(), mode=mode, auto=auto, allowed_by=allowed_by)

    if auto:
        logger.info("确认门放行（%s）：%s | 建议=%s", allowed_by or mode, file_id, result.level)
        return _stage_ingest(file_id, settings, forced=False)

    detail = result.headline + "：" + (result.reasons[0] if result.reasons else DETAIL_REVIEW_FALLBACK)
    logger.info("等待确认入库：%s（建议=%s，模式=%s）", file_id, result.level, mode)
    _touch_step(file_id, STEP_REVIEW)
    return _update(
        file_id,
        state=STATE_REVIEW,
        review_since=_now(),
        skipped_at="",
        detail=detail,
    )


def _stage_skip(file_id: str, *, detail: str = DETAIL_SKIPPED) -> Dict[str, Any]:
    """跳过：不提交知识库，但保留原始文本与档案（用户随时可以「仍要入库」）。"""
    _drop_knowledge_archive(file_id)
    # Phase 17：没进知识库的作业不该继续占着文档名，否则名字会被它锁住
    gallery_service.forget(file_id)
    _patch_section(
        file_id, "lightrag", state="skipped", error="已按你的选择跳过，未提交知识库"
    )
    logger.info("用户选择跳过入库：%s", file_id)
    return _update(
        file_id, state=STATE_SKIPPED, detail=detail, skipped_at=_now(), knowledge_path=""
    )


def _stage_ocr(file_id: str, settings, *, triggered_by: str) -> Dict[str, Any]:
    """整篇 OCR（PDF 扫描件 / 图片）：成功后重新质检。"""
    data = read_result(file_id) or {}

    if not settings.ocr_enabled:
        return _update(
            file_id,
            state=STATE_NEEDS_OCR,
            detail=(
                "OCR 未启用（OCR_MODE=off 或未选择可用的 OCR 提供方），本文件不会外发识别；"
                "可在设置页开启后重试"
            ),
        )

    source = _source_file(data)
    if source is None or not source.is_file():
        return _update(
            file_id,
            state=STATE_FAILED,
            detail="找不到上传的原件副本，无法进行 OCR（请重新上传）",
        )

    _update(
        file_id,
        state=STATE_OCR,
        detail=f"已提交 OCR（{settings.ocr_model or settings.ocr_provider}），正在识别…",
    )
    _touch_step(file_id, STEP_OCR)

    def on_progress(state: str, total: int, extracted: int) -> None:
        if state in ("pending", "running"):
            _update(
                file_id,
                state=STATE_OCR,
                detail=f"云端识别中（{state}）：{extracted}/{total or '?'} 页",
            )

    outcome = ocr_service.run_ocr(
        source,
        data.get("name") or source.name,
        content_type=data.get("content_type") or "",
        settings=settings,
        on_progress=on_progress,
    )

    _patch_section(
        file_id,
        "ocr",
        used=bool(outcome.job_id) or outcome.ok,
        provider=outcome.provider or settings.ocr_provider,
        model=outcome.model,
        job_id=outcome.job_id,
        scope=OCR_SCOPE_DOCUMENT,
        pages=outcome.pages,
        total_pages=outcome.total_pages,
        images=outcome.images,
        elapsed_s=round(outcome.elapsed_s, 1),
        error=None if outcome.ok else outcome.detail,
        triggered_by=triggered_by,
    )

    if not outcome.ok:
        logger.warning("OCR 失败：%s → %s", file_id, outcome.detail)
        return _update(file_id, state=STATE_FAILED, detail=outcome.detail)

    _write_content(file_id, outcome.text)
    _mark_assets_included(file_id)  # 整篇 OCR 已覆盖文档中的图片
    _update(
        file_id,
        pages=outcome.pages or int(data.get("pages") or 1),
        images=outcome.images,
        detail=f"OCR 完成：{outcome.pages} 页、{len(outcome.text)} 个字符，正在复检…",
    )
    # 复检用 route="text"：OCR 已把图像变成文字，不应再因"每页字符少"回到 needs_ocr
    return _stage_quality(
        file_id,
        settings,
        outcome.text,
        route="text",
        pages=outcome.pages or 1,
        allow_auto_ocr=False,
        images_settled=True,
    )


def _stage_ocr_images(file_id: str, settings, *, triggered_by: str) -> Dict[str, Any]:
    """内嵌图片 OCR：逐张识别 → 追加到 content.md → 复检（保守策略：只有点了才会外发）。"""
    data = read_result(file_id) or {}
    assets = list(data.get("assets") or [])
    pending = [asset for asset in assets if not _asset_ocr_done(asset)]

    if not settings.ocr_enabled:
        _patch_section(file_id, "ocr", scope=OCR_SCOPE_IMAGES)
        return _update(
            file_id,
            state=STATE_NEEDS_OCR,
            detail=(
                "OCR 未启用（OCR_MODE=off 或未选择可用的 OCR 提供方）："
                "图片不会外发识别，可点「仍要入库」只入库正文"
            ),
        )

    if not pending:
        return _stage_quality(
            file_id,
            settings,
            _read_content(file_id),
            route="text",
            pages=int(data.get("pages") or 1),
            allow_auto_ocr=False,
            images_settled=True,
        )

    _update(
        file_id,
        state=STATE_OCR,
        detail=f"正在识别内嵌图片：共 {len(pending)} 张…",
    )
    _touch_step(file_id, STEP_OCR)

    recognized: List[str] = []
    refreshed: List[Dict[str, Any]] = []
    failures: List[str] = []

    for index, asset in enumerate(assets, start=1):
        if _asset_ocr_done(asset):
            refreshed.append(asset)
            continue

        name = str(asset.get("name") or f"image{index}")
        path = _asset_file(file_id, asset)
        entry = dict(asset)
        entry_ocr = dict(asset.get("ocr") or {})

        if path is None or not path.is_file():
            entry_ocr.update({"done": False, "error": "文件已丢失（data/parsed 被清理？）"})
            entry["ocr"] = entry_ocr
            failures.append(f"{name}：文件已丢失")
            refreshed.append(entry)
            continue

        _update(
            file_id,
            state=STATE_OCR,
            detail=f"正在识别内嵌图片（{index}/{len(assets)}）：{name}…",
        )
        outcome = ocr_service.run_ocr(
            path,
            name,
            content_type=str(asset.get("content_type") or ""),
            settings=settings,
        )

        if outcome.ok:
            text = outcome.text.strip()
            entry_ocr.update(
                {
                    "done": True,
                    "chars": len(text),
                    "detail": outcome.detail,
                    "error": None,
                    "provider": outcome.provider or settings.ocr_provider,
                }
            )
            recognized.append(
                f"## 图片 {index}（{name}）的文字\n\n{text}"
            )
        else:
            entry_ocr.update(
                {
                    "done": False,
                    "chars": 0,
                    "error": outcome.detail,
                    "provider": outcome.provider or settings.ocr_provider,
                }
            )
            failures.append(f"{name}：{outcome.detail}")

        entry["ocr"] = entry_ocr
        refreshed.append(entry)

    _patch_section(
        file_id,
        "ocr",
        used=True,
        provider=settings.ocr_provider,
        model=settings.ocr_model,
        scope=OCR_SCOPE_IMAGES,
        images=len(assets),
        recognized_images=sum(1 for asset in refreshed if _asset_ocr_done(asset)),
        error=None if recognized else ("；".join(failures)[:300] or None),
        triggered_by=triggered_by,
    )
    _update(file_id, assets=refreshed)

    if not recognized:
        logger.warning("内嵌图片 OCR 全部失败：%s | %s", file_id, failures)
        return _update(
            file_id,
            state=STATE_FAILED,
            detail="内嵌图片识别失败：" + ("；".join(failures)[:300] or "未识别出任何文字"),
        )

    merged = _append_image_text(file_id, recognized)
    _update(
        file_id,
        detail=f"已识别 {len(recognized)} 张内嵌图片，正在复检合并后的内容…",
    )
    return _stage_quality(
        file_id,
        settings,
        merged,
        route="text",
        pages=int(data.get("pages") or 1),
        allow_auto_ocr=False,
        images_settled=True,
    )


def _stage_ingest(file_id: str, settings, *, forced: bool, confirmed: bool = False) -> Dict[str, Any]:
    """入库（本模块是唯一会调用 lightrag.upload 的地方）。

    **闸门不变量**：除非用户显式点「仍要入库」（forced=True），
    否则只有质检结论为 pass 的作业才允许走到这里
    （Phase 15：质检通过后还要过确认门，confirmed=True 表示这是用户点的「入库」）。
    """
    data = read_result(file_id) or {}
    quality = data.get("quality") or {}
    level = str(quality.get("level") or "")
    if not forced and level != quality_service.LEVEL_PASS:
        logger.error(
            "闸门不变量被触发：file_id=%s quality.level=%r 试图直接入库（已阻止）",
            file_id,
            level,
        )
        _patch_section(file_id, "lightrag", state="skipped", error=GATE_INVARIANT_DETAIL)
        return _update(file_id, state=STATE_FAILED, detail=GATE_INVARIANT_DETAIL)

    text = _read_content(file_id)

    if not text.strip():
        return _update(
            file_id, state=STATE_FAILED, detail="没有可入库的文本内容，请点击重试或重新上传"
        )

    _update(file_id, state=STATE_INGESTING, detail="已提交知识库，正在解析并构建索引…")
    _touch_step(file_id, STEP_SUBMIT)

    # Phase 17：文档名由 doc_name_for 统一给出（图片资料用用户起的名字，其余保持老规则）
    markdown_name = doc_name_for(data)
    payload = text.encode("utf-8")

    # Phase 17：重名守卫 —— 上游**允许**同名文档（真机实测知识库里躺着 3 份同名
    # `图文测试.md`），所以「不撞名」只能由我们保证；撞了就停在原地讲清出路，
    # 绝不悄悄传上去让别人（或自己下次提问）分不清哪份是哪份。
    conflict = doc_name_conflict(markdown_name, data)
    if conflict:
        logger.warning("文档名冲突，未提交知识库：%s | %s", file_id, markdown_name)
        _patch_section(file_id, "lightrag", state="skipped", error=conflict)
        keep_review = str(data.get("state") or "") == STATE_REVIEW
        return _update(
            file_id,
            state=STATE_REVIEW if keep_review else STATE_FAILED,
            detail=conflict,
        )

    # Phase 15.1：先问知识库收不收，收下了才留档。
    # 以前是「先留档再上传」，被 409 拒绝的文件也会在 knowledge/ 里留下一份，
    # 看起来像入库成功了 —— 现在「未入库 = 没留档」始终成立。
    response = lightrag.upload(markdown_name, payload, "text/markdown")  # ★ 唯一入库出口

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:BODY_PREVIEW_LIMIT]}

    http_ok = 200 <= response.status_code < 300
    track_id = str((body or {}).get("track_id") or "") if isinstance(body, dict) else ""
    raw_text = str(getattr(response, "text", "") or "").strip()
    raw_error = "" if http_ok else (raw_text[:BODY_PREVIEW_LIMIT] or str(body)[:300])

    _patch_section(
        file_id,
        "lightrag",
        state="submitted" if http_ok else "failed",
        track_id=track_id,
        http_status=int(response.status_code),
        error=None if http_ok else raw_error,
    )

    if not http_ok:
        # Phase 15.1：说人话 ——「HTTP 409」对用户没有意义，要讲清「发生了什么 + 下一步怎么做」；
        # 原始返回仍保留在 lightrag.error 里（网页上可展开查看）。
        reason = humanize_upstream(response.status_code, raw_text or str(body))
        logger.warning(
            "知识库拒绝接收：%s HTTP %s | %s", file_id, response.status_code, reason
        )
        # 「没入库 = 没留档」：knowledge\ 里若还留着这个作业的历史残留（旧版本「先留档再上传」的产物），
        # 这里一并回收 —— 否则用户会以为它已经进知识库了。
        _drop_knowledge_archive(file_id)
        return _update(file_id, state=STATE_FAILED, detail=reason, knowledge_path="")

    _save_knowledge(file_id, markdown_name, payload)
    # Phase 17：落定文档名 + 登记「名字 ↔ 作业」映射（双击打开原图、按行删除都靠它）。
    # 索引本身是**派生数据**（result.json 里什么都有，丢了能重建），所以这里不再多一道失败面。
    _update(file_id, doc_name=markdown_name)
    gallery_service.register(
        markdown_name,
        file_id,
        name=str(data.get("name") or ""),
        sha256=str(data.get("sha256") or ""),
        evidence=evidence_service.from_job(data, doc_name=markdown_name, content=text),
    )

    if forced:
        # 人工放行必须留痕：谁在什么时候、基于什么原因强行入库
        _patch_section(
            file_id,
            "quality",
            forced=True,
            forced_at=_now(),
            forced_reasons=list(quality.get("reasons") or []),
        )

    logger.info(
        "已入库：%s（%s，%s 字符，强制=%s，track_id=%s）",
        file_id,
        markdown_name,
        len(text),
        forced,
        track_id or "-",
    )
    note = GATE_FORCED_NOTE if forced else (GATE_CONFIRMED_NOTE if confirmed else "")
    _touch_step(file_id, STEP_INDEX)
    # review_since 清空：确认门已经走完了，避免被启动时的超时归档当成「还在等确认」
    return _update(
        file_id,
        state=STATE_DONE,
        review_since="",
        detail=f"已提交知识库{note}，正在构建索引；稍后即可提问相关内容",
    )


# ============================================================
# 文件辅助
# ============================================================

def _source_file(data: Dict[str, Any]) -> Optional[Path]:
    """原件副本路径（result.json 中保存的绝对路径）。"""
    absolute = str(data.get("source_path") or "")
    return Path(absolute) if absolute else None


def _read_content(file_id: str) -> str:
    path = content_path(file_id)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_content(file_id: str, text: str) -> None:
    path = content_path(file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _append_image_text(file_id: str, blocks: List[str]) -> str:
    """把图片 OCR 结果追加到 content.md，返回合并后的全文（复检与入库都用它）。"""
    current = _read_content(file_id).rstrip()
    addition = "\n\n".join(blocks).strip()
    if not addition:
        return current
    merged = (current + "\n\n<!-- 以下文字由内嵌图片 OCR 得到 -->\n\n" + addition).strip()
    _write_content(file_id, merged)
    return merged


def _drop_knowledge_archive(file_id: str) -> str:
    """回收这个作业在 knowledge/ 里可能残留的留档（Phase 15.1），返回被删掉的文件名。

    「没入库 = 没留档」必须在 knowledge/ 里看得见。历史上因为「先留档再上传」加上连点竞态，
    被知识库拒绝、或者最终被跳过（state=skipped）的作业也留下过文件，这里顺手清理：
    点「跳过」时清，上传被拒时也清。清理失败只告警，不影响主流程。
    """
    removed = ""
    try:
        for path in sorted(KNOWLEDGE_DIR.glob(f"{safe_filename(file_id)}_*")):
            if path.is_file():
                path.unlink()
                removed = path.name
    except OSError as exc:
        logger.warning("清理残留留档失败（不影响主流程）：%s", exc)
    if removed:
        logger.warning("回收了残留留档：%s（%s）", removed, file_id)
    return removed


def _save_knowledge(file_id: str, markdown_name: str, payload: bytes) -> str:
    """把 Markdown 留档到 knowledge/（失败不影响入库）。

    注意：Phase 15.1 起只在**知识库确认接收之后**调用（见 _stage_ingest），
    被拒绝的文件不留档 —— 这样「knowledge/ 里有文件」就等于「知识库确实收下了」。
    """
    try:
        KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
        target = KNOWLEDGE_DIR / f"{safe_filename(file_id)}_{markdown_name}"
        target.write_bytes(payload)
        _update(file_id, knowledge_path=str(target.name))
        logger.info("文本已留档：%s", target)
        return str(target.name)
    except OSError as exc:
        logger.warning("文本留档失败（不影响入库）：%s", exc)
        return ""


# ---------- Phase 16：删除功能要用到的两个入口 ----------

def drop_knowledge_archive(file_id: str) -> str:
    """（公开入口）回收这个作业在 knowledge/ 里的留档，返回被删掉的文件名。

    与内部的 `_drop_knowledge_archive` 是**同一份实现**：删除功能、「跳过」、
    「上传被拒」三条路径必须走同一条清理逻辑 —— 只要有一处不一样，
    「knowledge/ 里有文件 = 知识库确实收下了」这个约定就会在某条路径上失效。
    """
    return _drop_knowledge_archive(file_id)


def mark_removed_from_kb(file_id: str, *, detail: str = "") -> Dict[str, Any]:
    """把「刚从知识库删除」记进档案（Phase 16，只改状态、**不碰任何文件**）。

    为什么需要它：只删知识库记录（范围 = 仅知识库）时本机留档仍然在，如果档案里的
    state 还写着 done，文件管理页就会把它当成「已入库」而漏掉「本机有、知识库没有」
    的提醒。文件删不删由删除范围决定，这里只说清「知识库里已经没有了」。
    """
    _patch_section(file_id, "lightrag", state="deleted", error="已按你的选择从知识库删除")
    # Phase 17：知识库里已经没有这份文档了，名字也该放出来给别人用（删除后重新入库不受阻）
    gallery_service.forget(file_id)
    logger.warning("从知识库删除并记入档案：%s", file_id)
    return _update(file_id, state=STATE_REMOVED, detail=detail or DETAIL_REMOVED_FROM_KB)


# ============================================================
# 内嵌图片（Phase 9）：落盘 → 记录 → OCR 回填
# ============================================================

def _asset_ocr_done(asset: Dict[str, Any]) -> bool:
    return bool((asset.get("ocr") or {}).get("done"))


def _asset_file(file_id: str, asset: Dict[str, Any]) -> Optional[Path]:
    relative = str(asset.get("path") or "")
    if not relative:
        return None
    return job_dir(file_id) / relative


def _pending_images(data: Dict[str, Any]) -> int:
    """还没识别（或识别失败）的内嵌图片数量。"""
    return sum(1 for asset in (data.get("assets") or []) if not _asset_ocr_done(asset))


def _image_stop_reason(settings, data: Dict[str, Any]) -> str:
    """有未识别图片时返回需要用户确认的原因；返回空串表示可以直接入库。

    保守策略：默认（QUALITY_MAX_UNOCR_IMAGES=0）一张未识别的图片都不放行，
    必须由用户点「识别图片」或「仍要入库」；把该值调大即可容忍少量图片。
    """
    if not getattr(settings, "ocr_image_ocr", True):
        return ""
    tolerance = int(getattr(settings, "quality_max_unocr_images", 0) or 0)
    pending = _pending_images(data)
    if pending <= max(tolerance, 0):
        return ""
    return (
        f"文档正文已通过质检，但还有 {pending} 张内嵌图片没有识别："
        "点「识别图片」把图片文字补进来，或点「仍要入库」只入库正文"
    )


def _store_assets(file_id: str, assets: List[Any]) -> List[Dict[str, Any]]:
    """把解析阶段抽出的内嵌图片落盘，返回要写进 result.json 的清单。"""
    directory = assets_dir(file_id)
    stored: List[Dict[str, Any]] = []

    for index, asset in enumerate(assets or []):
        payload = bytes(getattr(asset, "data", b"") or b"")
        name = safe_filename(str(getattr(asset, "name", "") or f"image{index + 1}.bin"))
        relative = f"{ASSETS_DIR}/{index:02d}_{name}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (job_dir(file_id) / relative).write_bytes(payload)
        except OSError as exc:
            logger.warning("内嵌图片落盘失败（忽略这一张）：%s", exc)
            continue
        stored.append(
            {
                "order": index,
                "name": name,
                "source": str(getattr(asset, "source", "") or ""),
                "path": relative,
                "bytes": len(payload),
                "ocr": {"done": False, "chars": 0, "provider": "", "error": None},
            }
        )

    if stored:
        logger.info(
            "内嵌图片已落盘：%s（%d 张，%d 字节）",
            file_id,
            len(stored),
            sum(int(item["bytes"]) for item in stored),
        )
    return stored


def _mark_assets_included(file_id: str) -> None:
    """整篇 OCR 已经覆盖文档里的图片：标记为已处理，避免再次停下要求识别。"""
    data = read_result(file_id) or {}
    assets = list(data.get("assets") or [])
    if not assets:
        return

    changed = False
    for asset in assets:
        block = dict(asset.get("ocr") or {})
        if block.get("done"):
            continue
        block.update(
            {"done": True, "chars": 0, "included_in_document": True, "error": None}
        )
        asset["ocr"] = block
        changed = True

    if changed:
        _update(file_id, assets=assets)


# ============================================================
# 对外接口（供路由使用）
# ============================================================

def confirm_mode() -> str:
    """当前生效的确认门取值（供 GET /ingest/pending 告知前端）。"""
    return _confirm_mode(get_settings())


def session_allow_all() -> bool:
    """本次运行是否已「全部允许」（进程重启后自动复位）。"""
    with _session_lock:
        return _session_allow_all


def set_session_allow_all(enabled: bool) -> Dict[str, Any]:
    """打开 / 关闭「本次运行内全部允许」。

    打开时顺带把**已经在等确认**的作业一起放行（用户点这个开关的意图就是
    「别一个个问了」；只对新文件生效会让人以为开关没生效）。
    返回 {allow_all, released, message}，供接口回执给前端。
    """
    global _session_allow_all
    with _session_lock:
        _session_allow_all = bool(enabled)

    released = 0
    if enabled:
        for item in pending_reviews():
            enqueue(str(item.get("file_id") or ""), ACTION_INGEST)
            released += 1
        logger.info("本次运行内全部允许：已放行 %d 个等待确认的作业", released)
    else:
        logger.info("已关闭「本次运行内全部允许」：后续文件仍会逐个确认")

    return {
        "allow_all": bool(enabled),
        "released": released,
        "message": (
            f"已打开：本次运行内新文件直接入库（已放行 {released} 个等待确认的文件；"
            "重启服务后恢复逐个确认）"
            if enabled
            else "已关闭：后续文件会继续逐个确认"
        ),
    }


def _review_item(data: Dict[str, Any]) -> Dict[str, Any]:
    """把一份处于 review 的档案压成列表项（GET /ingest/pending 用）。"""
    metrics = (data.get("quality") or {}).get("metrics") or {}
    return {
        "file_id": data.get("file_id"),
        "name": data.get("name"),
        "ext": data.get("ext"),
        "size": data.get("size"),
        "route": data.get("route"),
        "route_label": data.get("route_label"),
        "pages": data.get("pages"),
        "content_chars": metrics.get("char_total") or 0,
        "detail": data.get("detail") or "",
        "review_since": data.get("review_since") or data.get("updated_at") or "",
        "advice": data.get("advice") or {},
    }


def pending_reviews() -> List[Dict[str, Any]]:
    """列出所有「等待确认入库」的作业（按进入确认门的时间倒序，新的在前）。"""
    items: List[Dict[str, Any]] = []
    try:
        directories = [path for path in PARSED_DIR.iterdir() if path.is_dir()]
    except OSError as exc:  # 目录不存在 / 无权限：当没有待确认作业处理
        logger.warning("读取解析目录失败：%s", exc)
        return items

    for directory in directories:
        data = read_result(directory.name)
        if not data or str(data.get("state") or "") != STATE_REVIEW:
            continue
        items.append(_review_item(data))

    items.sort(key=lambda item: str(item.get("review_since") or ""), reverse=True)
    return items


def active_jobs() -> List[Dict[str, Any]]:
    """列出当前仍在解析 / 校验 / OCR / 入库的作业（页面刷新后恢复真实状态）。

    只读取既有 result.json；不查询 LightRAG，因此不会把状态接口变慢。更细的
    建索引进度仍由前端对单个 file_id 调 /ingest/status 获取。
    """
    items: List[Dict[str, Any]] = []
    try:
        directories = [path for path in PARSED_DIR.iterdir() if path.is_dir()]
    except OSError:
        return items

    for directory in directories:
        data = read_result(directory.name)
        if not data or str(data.get("state") or "") not in BUSY_STATES:
            continue
        items.append(
            {
                "file_id": str(data.get("file_id") or directory.name),
                "name": str(data.get("name") or directory.name),
                "state": str(data.get("state") or ""),
                "detail": str(data.get("detail") or ""),
                "route_label": str(data.get("route_label") or ""),
                "updated_at": str(data.get("updated_at") or ""),
            }
        )
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return items


def _parse_stamp(text: str) -> Optional[datetime]:
    """解析档案里的时间戳（形如 2026-10-15 21:03:11）；看不懂就返回 None。"""
    try:
        return datetime.strptime(str(text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def archive_expired_reviews(ttl_hours: Optional[int] = None) -> List[str]:
    """把等确认超时的作业归档为「已跳过」（服务启动时调用一次，Phase 15）。

    为什么要有超时：确认门是「停下来等人」的状态，如果一直没有答复，
    作业目录会越堆越多，用户下次打开页面会看到一堆不知何时上传的文件。
    超过 `PENDING_REVIEW_TTL_HOURS`（默认 24 小时，0 = 不归档）的作业
    一律按「跳过」处理 —— **跳过只是不入库，原件与解析结果都还在**，
    用户想入库随时可以点「仍要入库」，不存在"过期就丢了"的风险。
    """
    settings = get_settings()
    hours = settings.pending_review_ttl_hours if ttl_hours is None else int(ttl_hours)
    if hours <= 0:
        return []

    deadline = datetime.now() - timedelta(hours=hours)
    archived: List[str] = []
    for item in pending_reviews():
        stamp = _parse_stamp(str(item.get("review_since") or ""))
        if stamp is None or stamp > deadline:
            continue
        file_id = str(item.get("file_id") or "")
        if not file_id:
            continue
        _stage_skip(file_id, detail=DETAIL_EXPIRED.format(hours=hours))
        archived.append(file_id)

    if archived:
        logger.info(
            "等待确认超过 %d 小时的作业已归档为跳过（未入库）：%s",
            hours,
            "、".join(archived),
        )
    return archived


def activate(file_id: str, action: str) -> Dict[str, Any]:
    """处理用户动作：ocr / force / retry / ingest / skip。返回 {ok, http_status, message, state}。"""
    if action not in ACTIONS:
        return {
            "ok": False,
            "http_status": 400,
            "message": f"不支持的动作：{action}（可选 {', '.join(ACTIONS)}）",
        }

    data = read_result(file_id)
    if not data:
        return {"ok": False, "http_status": 404, "message": "找不到这个文件的处理记录"}

    state = str(data.get("state") or "")
    if state in BUSY_STATES:
        return {"ok": False, "http_status": 409, "message": "该文件正在处理中，请稍候"}

    if action == ACTION_OCR:
        if not get_settings().ocr_enabled:
            return {
                "ok": False,
                "http_status": 409,
                "message": (
                    "OCR 未启用：请在设置页（或 .env）把 OCR_MODE 设为 manual/auto，"
                    "并选择可用的 OCR 提供方"
                ),
            }
        if state != STATE_NEEDS_OCR:
            return {"ok": False, "http_status": 409, "message": "当前状态不需要 OCR 识别"}

    if action == ACTION_FORCE:
        if state == STATE_DONE:
            return {"ok": False, "http_status": 409, "message": "该文件已经入库"}
        if not _read_content(file_id).strip():
            return {
                "ok": False,
                "http_status": 409,
                "message": "本文件没有可入库的文字内容，无法强制入库",
            }

    if action == ACTION_RETRY and state == STATE_DONE:
        # 已 done 的文件再跑一遍会重复入库（同一个 file_id 会产生第二份文档）
        return {
            "ok": False,
            "http_status": 409,
            "message": "该文件已经入库，重复处理会产生重复文档；如需重新入库请重新上传",
        }

    if action == ACTION_FORCE and state == STATE_REVIEW:
        # Phase 15：已经通过质检的作业该走「入库」确认这条路。
        # 若允许用「强制入库」，档案里会留下 forced 记录（看起来像绕过了质检），
        # 与 can.force=false（页面上不显示这个按钮）也对不上，所以在这里挡住。
        return {
            "ok": False,
            "http_status": 409,
            "message": "该文件已通过质检、正在等你确认：请点「入库」确认，或点「跳过」不入库",
        }

    if action == ACTION_INGEST and state != STATE_REVIEW:
        # Phase 15：只有停在确认门上的作业才需要用户点「入库」
        # （已经入库的不必再点；未通过质检的要走「仍要入库」才会留痕）
        return {
            "ok": False,
            "http_status": 409,
            "message": "该文件当前不在等待确认入库",
        }

    if action == ACTION_SKIP and state != STATE_REVIEW:
        return {
            "ok": False,
            "http_status": 409,
            "message": "该文件当前不在等待确认入库",
        }

    if not enqueue(file_id, action):
        # Phase 15.1 幂等：同一个动作已经排过队了（用户在页面上连点），
        # 这次请求不重复执行；前端会提示「这次点击已忽略」并继续轮询真实状态。
        return {
            "ok": True,
            "http_status": 200,
            "state": state,
            "duplicate_submit": True,
            "message": "这次点击已忽略：上一次同样的操作还在处理中",
        }

    message = {
        ACTION_INGEST: "已确认入库，正在提交知识库",
        ACTION_SKIP: "已跳过：本文件不会入库",
    }.get(action, "已开始处理")
    return {
        "ok": True,
        "http_status": 200,
        "state": state,
        "duplicate_submit": False,
        "message": message,
    }


def status(file_id: str) -> Optional[Dict[str, Any]]:
    """汇总一个作业的状态（供 GET /ingest/status/{file_id}）。"""
    data = read_result(file_id)
    if not data:
        return None

    settings = get_settings()
    state = str(data.get("state") or "")
    content = _read_content(file_id)
    has_text = bool(content.strip())
    metrics = (data.get("quality") or {}).get("metrics") or {}
    assets = list(data.get("assets") or [])
    pending_images = _pending_images(data)
    image_total = len(assets)

    return {
        "file_id": data.get("file_id"),
        "name": data.get("name"),
        "ext": data.get("ext"),
        "size": data.get("size"),
        "route": data.get("route"),
        "route_label": data.get("route_label"),
        "pages": data.get("pages"),
        "images": data.get("images"),
        "state": state,
        "detail": data.get("detail") or "",
        "quality": data.get("quality") or {},
        "ocr": data.get("ocr") or {},
        "lightrag": data.get("lightrag") or {},
        # Phase 15：入库建议（等待确认时前端据此展示「为什么建议你看一眼」）
        "advice": data.get("advice") or {},
        # Phase 17：图片资料的文档名（页面显示「将入库为 …」）与「这段描述是我写的」
        "doc_name": doc_name_for(data) if (is_image_job(data) or data.get("doc_name")) else "",
        "is_image": is_image_job(data),
        "manual_text": bool(data.get("manual_text")),
        "review_since": data.get("review_since") or "",
        "skipped_at": data.get("skipped_at") or "",
        "content_chars": metrics.get("char_total") or 0,
        "assets": [
            {
                "order": asset.get("order"),
                "name": asset.get("name"),
                "source": asset.get("source"),
                "bytes": asset.get("bytes"),
                "ocr_done": _asset_ocr_done(asset),
                "chars": int((asset.get("ocr") or {}).get("chars") or 0),
                "error": (asset.get("ocr") or {}).get("error"),
            }
            for asset in assets
        ],
        "image_total": image_total,
        "unocr_images": pending_images,
        "ocr_mode": settings.ocr_mode,
        "ocr_provider": settings.ocr_provider,
        "ocr_available": settings.ocr_enabled,
        "ocr_enabled": settings.ocr_enabled,
        "ocr_external": settings.ocr_external,
        "ocr_image_ocr": bool(settings.ocr_image_ocr),
        "can": {
            "ocr": state == STATE_NEEDS_OCR and settings.ocr_enabled,
            # 「仍要入库」是「我知道没过质检，但还是入库」的兜底动作，必须留痕；
            # 等确认的作业（review）该点「入库」，不走这条（避免误留 forced 记录）
            "force": state not in (STATE_DONE, STATE_REVIEW)
            and state not in BUSY_STATES
            and has_text,
            "retry": state in RETRYABLE_STATES,
            # Phase 15：确认门上的两个动作
            "ingest": state == STATE_REVIEW,
            "skip": state == STATE_REVIEW,
            # Phase 15.2：能不能改正文（忙 / 已入库 / 还没有正文时不给按钮）
            "edit": not _edit_reason(file_id, data, content),
        },
        # Phase 15.2：处理过程时间线（每步在做什么 / 花了多久 / 现在到哪）
        "timeline": _readable_timeline(data),
        "timeline_elapsed_s": _elapsed_from_accepted(data),
        "edit": _edit_summary(data),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


# ============================================================
# Phase 15.2：入库过程可读化（处理过程时间线）
# ============================================================
#
# 为什么要它：以前卡片上只有一句 detail（「正在本地解析…」）+ 一个徽章，
# 用户看不出「现在到哪一步了、这一步花了多久、卡在哪」——尤其入库慢的时候
# 只能干等。现在每一步的开始时间都落在 result.json 的 timeline 段里，
# status() 把它压成一行一步的过程表（谁做的、什么时候、多久、现在在哪），
# 打开页面/刷新都能看到同一份过程。
#
# 设计取舍：**只记录、只展示，不参与任何判定** —— 时间线缺失或写失败都不影响
# 解析与入库（_touch_step 内部吞掉异常），因此这里没有引入新的失败面。

def _timeline_stamps(data: Dict[str, Any]) -> Dict[str, str]:
    """收齐各步骤的开始时间（timeline 段为主，created_at 兜底）。"""
    stamps = {
        str(key): str(value or "")
        for key, value in (data.get("timeline") or {}).items()
        if value
    }
    accepted = str(data.get("created_at") or "")
    if accepted and not stamps.get(STEP_ACCEPTED):
        stamps[STEP_ACCEPTED] = accepted
    return stamps


def _current_step(state: str, data: Dict[str, Any]) -> str:
    """当前正停在哪一步（时间线据此标「进行中」，并把没到的步骤标成「待开始」）。"""
    if state == STATE_PARSING:
        return STEP_PARSING
    if state == STATE_CHECKING:
        return STEP_CHECKING
    if state in (STATE_OCR, STATE_NEEDS_OCR):
        return STEP_OCR
    if state in (STATE_REVIEW, STATE_SKIPPED):
        return STEP_REVIEW
    if state == STATE_INGESTING:
        return STEP_SUBMIT
    if state == STATE_DONE:
        return STEP_INDEX
    if state == STATE_BLOCKED:
        return STEP_CHECKING
    if state == STATE_FAILED:
        # 失败发生在哪一步？按档案里留下的痕迹判断：
        # 知识库明确回了错误 → 提交那一步；OCR 报了错 → 识别那一步；否则算解析失败。
        lightrag = data.get("lightrag") or {}
        if str(lightrag.get("state") or "") == "failed" or int(lightrag.get("http_status") or 0) >= 400:
            return STEP_SUBMIT
        if (data.get("ocr") or {}).get("error"):
            return STEP_OCR
        return STEP_PARSING
    return ""


def _step_relevant(key: str, stamps: Dict[str, str], current: str) -> bool:
    """这一步要不要出现在过程表上：没发生过的可选步骤（OCR/确认/提交）不显示。"""
    if key in (STEP_OCR, STEP_REVIEW, STEP_SUBMIT, STEP_INDEX):
        return bool(stamps.get(key)) or current == key
    return True


def _first_reason(data: Dict[str, Any]) -> str:
    """第一条质检原因（人话，用于过程表与提示语）。"""
    reasons = (data.get("quality") or {}).get("reasons") or []
    return str(reasons[0]) if reasons else ""


def _step_hint(key: str, state: str, data: Dict[str, Any], current: str) -> str:
    """每一步右侧那句人话（说清「在做什么 / 结果如何 / 要不要你做点什么」）。"""
    quality = data.get("quality") or {}
    metrics = quality.get("metrics") or {}
    lightrag = data.get("lightrag") or {}
    ocr = data.get("ocr") or {}

    if key == STEP_ACCEPTED:
        return "文件已收到，排队等待处理"
    if key == STEP_PARSING:
        if current == STEP_PARSING:
            return "正在提取文字…"
        return "解析完成（%s，%s 字）" % (
            data.get("route_label") or "本地解析",
            metrics.get("char_total") or 0,
        )
    if key == STEP_CHECKING:
        if current == STEP_CHECKING and state == STATE_BLOCKED:
            return "未通过：%s" % (_first_reason(data) or "内容质量不达标")
        if current == STEP_CHECKING:
            return "正在判定文字能不能用…"
        if quality.get("forced"):
            return "通过（人工放行，质量分 %s）" % (quality.get("score") or 0)
        return "通过（质量分 %s）" % (quality.get("score") or 0)
    if key == STEP_OCR:
        if current == STEP_OCR:
            if state == STATE_NEEDS_OCR:
                return "还没开始：点「OCR 解析」或「识别图片」才会外发识别"
            return "正在识别…"
        if str(ocr.get("scope") or "") == "images":
            return "已识别 %s/%s 张内嵌图片" % (
                ocr.get("recognized_images") or 0,
                ocr.get("images") or (data.get("images") or 0),
            )
        return "整篇识别完成（%s 页）" % (ocr.get("pages") or data.get("pages") or 0)
    if key == STEP_REVIEW:
        if state == STATE_SKIPPED:
            return "已跳过：没进知识库（原件与解析结果都还在）"
        if current == STEP_REVIEW:
            return "等你点「入库 / 跳过」"
        return "已确认入库"
    if key == STEP_SUBMIT:
        if current == STEP_SUBMIT and state == STATE_FAILED:
            return str(data.get("detail") or "知识库拒绝了这次提交")[:60]
        if current == STEP_SUBMIT:
            return "正在上传解析结果…"
        if lightrag.get("track_id"):
            return "已提交知识库（提交号 %s…）" % str(lightrag.get("track_id"))[:8]
        return "已提交知识库"
    if key == STEP_INDEX:
        if current == STEP_INDEX:
            return "知识库正在解析与建索引，跑完就能提问"
        return "提交后由知识库自动完成"
    return ""


def _readable_timeline(data: Dict[str, Any], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """把档案压成「一步一行」的处理过程（GET /ingest/status 用）。

    纯读取：只看 result.json 里的状态与时间戳，不联网、不读盘、不参与判定。
    """
    stamps = _timeline_stamps(data)
    state = str(data.get("state") or "")
    current = _current_step(state, data)
    moment = now or datetime.now()
    keys = [key for key in STEP_KEYS if _step_relevant(key, stamps, current)]

    items: List[Dict[str, Any]] = []
    for index, key in enumerate(keys):
        stamp = stamps.get(key) or ""
        start = _parse_stamp(stamp)
        following = stamps.get(keys[index + 1]) if index + 1 < len(keys) else ""
        stop = _parse_stamp(following) if following else None

        if key == current:
            if state == STATE_SKIPPED:
                row = "skipped"
            elif state in (STATE_FAILED, STATE_BLOCKED):
                row = "failed"
            else:
                row = "current"
        elif start is not None:
            row = "done"
        else:
            row = "pending"

        elapsed: Optional[float] = None
        if start is not None:
            # 已完成的一步：下一步开始的时间 − 这一步开始的时间；
            # 正在进行的这一步：现在 − 开始时间（所以卡片上的秒数是活的）。
            end = stop if stop is not None else (
                moment if row in ("current", "failed", "skipped") else None
            )
            if end is not None and end >= start:
                elapsed = round((end - start).total_seconds(), 1)

        items.append(
            {
                "key": key,
                "label": STEP_LABELS.get(key, key),
                "state": row,
                "at": stamp,
                "elapsed_s": elapsed,
                "hint": _step_hint(key, state, data, current),
            }
        )
    return items


def _elapsed_from_accepted(data: Dict[str, Any]) -> float:
    """从「已受理上传」到现在的总用时（秒）；算不出来返回 0.0。"""
    start = _parse_stamp(_timeline_stamps(data).get(STEP_ACCEPTED, ""))
    if start is None:
        return 0.0
    return round(max(0.0, (datetime.now() - start).total_seconds()), 1)


# ============================================================
# Phase 15.2：先改正文，再入库
# ============================================================
#
# 为什么要有它：解析结果常常「基本对、但有点脏」——页眉页脚重复、目录页码、
# 表格串行、OCR 把「0」认成「O」。以前用户只有两条路：要么原样入库（脏数据
# 一起进），要么不入库（好内容也浪费）。现在可以就地在卡片上改：
#
#   GET  /ingest/content/<file_id>   取回当前正文（改之前总要能看见）
#   POST /ingest/content/<file_id>   保存改后的正文（可顺带直接入库）
#
# 三条不变量（改版最容易被破坏的地方，测试专门守）：
#   1. **只动 content.md**：原件（source.*）与上传留档（data/uploads/）永不改写；
#      改之前的上一版留在 content.before-edit.md，随时能对照。
#   2. **改完必须重新过质量门 + 重新给建议**：编辑不是绕过质检的后门，
#      改坏的正文照样会被拦下（blocked / needs_ocr）。
#   3. **入库仍然要用户点头**：除了显式「保存并入库」（等价于点「入库」），
#      任何编辑都不会自己调 lightrag.upload()；质检没过的正文一律不上传。

def _edit_reason(file_id: str, data: Dict[str, Any], text: Optional[str] = None) -> str:
    """不能改正文时的人话原因；返回空串 = 可以改（网页按钮与接口共用这一处判断）。

    `text` 是可选的已读正文：`status()` 已经读过一次，不必为了判断再读一遍磁盘。
    """
    state = str(data.get("state") or "")
    if state in BUSY_STATES:
        return "文件%s，做完这一步才能改" % STATE_LABELS.get(state, "正在处理")
    if state == STATE_DONE:
        return "已经入库了：改动不会同步到知识库；要改内容请重新上传一份"
    if text is None:
        text = _read_content(file_id)
    if not text.strip():
        # Phase 17：纯图片资料本来就没有可提取的文字（点「重试」也长不出文字来）——
        # 让用户直接写一段描述入库，比把他堵在「请先点重试」上更有意义。
        if is_image_job(data):
            return ""
        return "还没有可编辑的正文（解析没成功），请先点「重试」"
    return ""


def _edit_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """编辑留痕（网页上显示「已改过 N 次（最后一次 时间）」）。"""
    block = dict(data.get("edit") or {})
    return {
        "count": int(block.get("count") or 0),
        "edited_at": block.get("edited_at") or "",
        "before_chars": int(block.get("before_chars") or 0),
        "after_chars": int(block.get("after_chars") or 0),
        "last_ingest_after_edit": bool(block.get("ingest_after_edit")),
        "backup": BACKUP_NAME,
    }


def content_for_edit(file_id: str) -> Optional[Dict[str, Any]]:
    """取回可编辑的正文（GET /ingest/content/{file_id}）；作业不存在返回 None。"""
    data = read_result(file_id)
    if not data:
        return None
    text = _read_content(file_id)
    reason = _edit_reason(file_id, data, text)
    body, caption = _report_caption_split(text)
    image_job = is_image_job(data)
    return {
        "file_id": file_id,
        "name": data.get("name"),
        "state": str(data.get("state") or ""),
        "route": data.get("route"),
        "route_label": data.get("route_label"),
        "chars": len(text),
        "lines": len(text.splitlines()),
        "content": text,
        "editable": not reason,
        "reason": reason,
        "max_chars": CONTENT_MAX_CHARS,
        "edit": _edit_summary(data),
        # Phase 17：图片资料走「写描述」面板（图片没有文字，用户写的是描述 + 名字）
        "is_image": image_job,
        "doc_id": doc_id_of(data),
        "doc_name": doc_name_for(data),
        "doc_name_stem": doc_name_stem_of(data),
        "suggested_doc_name": suggested_doc_name(data) if image_job else "",
        "manual_text": bool(data.get("manual_text")),
        "caption_text": caption,
        "caption_chars": len(caption),
        "body_chars": len(body),
    }


def edit_content(
    file_id: str,
    text: str,
    *,
    ingest_now: bool = False,
    caption: bool = False,
    doc_name: str = "",
) -> Dict[str, Any]:
    """保存用户改过的正文，然后**重新质检**（可选：顺带入库）。

    返回 {ok, http_status, message, state, chars, edited, ingested, status}：
    * `ok=False` 时只有 `message` 是给人看的，路由按 `http_status` 回 4xx；
    * `status` 是最新的完整状态（`GET /ingest/status/{file_id}` 那份），
      前端不必再拉一次就能把卡片画新。

    注意「保存并入库」是**同步**执行的：它等价于用户先点「保存」再点「入库」，
    只是少一次点击。上传本身是一次 HTTP 调用（通常 1~3 秒）。

    Phase 17（图片资料）：
    * `caption=True` 表示 `text` 是**描述**：会追加到已有正文之后（描述块由
      `CAPTION_MARK` 标记；重复保存是"替换上一段描述"，不会越堆越多）；
    * `doc_name` 是用户给这份资料起的名字，与描述一起落盘，入库时成为上游文档名
      （`<名字>_<短ID>.md`，见 `make_doc_name`）；只有图片资料会用它。
    """
    data = read_result(file_id)
    if not data:
        return {"ok": False, "http_status": 404, "message": "找不到这个文件的处理记录"}

    current = _read_content(file_id)
    # 忙 / 已入库 / 还没有正文：与网页按钮用的是同一处判断，避免两边说法不一致
    reason = _edit_reason(file_id, data, current)
    if reason:
        return {"ok": False, "http_status": 409, "message": reason}

    image_job = is_image_job(data)
    # Phase 17：定下用户给这份资料起的名字（只有图片资料的「写描述」会传）。命名是**幂等**的：
    # 同一个名字再提交一次还是同一个名字，不会长出 `书桌照片_3fcb99_3fcb99.md`。
    if image_job and str(doc_name or "").strip():
        new_name = make_doc_name(doc_name, data)
        if new_name != str(data.get("doc_name") or ""):
            _update(file_id, doc_name=new_name)
            data = read_result(file_id) or data

    addition = ""
    if caption:
        # Phase 17：描述是**追加**到正文里的（图片的 OCR 文本 / 之前写的描述都不动），
        # 用户看的就是完整正文，质检与入库用的也是完整正文 —— 不存在第二份"描述"副本。
        addition = str(text or "").strip()
        if not addition:
            return {
                "ok": False,
                "http_status": 400,
                "message": "描述不能为空：不想入库请点「跳过」",
            }
        text = _append_caption(file_id, addition)
    else:
        text = str(text or "")

    if not text.strip():
        return {
            "ok": False,
            "http_status": 400,
            "message": "正文不能为空：不想入库请点「跳过」，改坏了可以点「放弃修改」",
        }
    if len(text) > CONTENT_MAX_CHARS:
        return {
            "ok": False,
            "http_status": 413,
            "message": "正文太长（%d 字，上限 %d 字）：请分成几份资料分别上传"
            % (len(text), CONTENT_MAX_CHARS),
        }
    if text == current:
        # 一个字都没动：不做无意义的重新质检（也就不会把档案搅乱）
        return {
            "ok": True,
            "http_status": 200,
            "action": "edit",
            "state": str(data.get("state") or ""),
            "chars": len(text),
            "edited": False,
            "ingested": False,
            "message": "正文没有变化，未做任何改动",
            "status": status(file_id),
        }

    # 1) 上一版留档（只留一份：随时能看到「改之前长什么样」）
    backup = job_dir(file_id) / BACKUP_NAME
    try:
        backup.write_text(current, encoding="utf-8")
    except OSError as exc:  # 备份失败不阻断保存，但要说清楚
        logger.warning("编辑前备份失败（不影响保存）：%s：%s", file_id, exc)

    # 2) 写回正文（原件与上传留档不动）；「写描述」在 _append_caption 里已经写过一次
    if not caption:
        _write_content(file_id, text)
    else:
        # Phase 17：记一笔「这段描述是我写的」（质检查不到的内容，靠人自己认领）
        _update(file_id, manual_text=True, manual_text_at=_now())
        _patch_section(
            file_id,
            "caption",
            count=int((data.get("caption") or {}).get("count") or 0) + 1,
            chars=len(addition),
            at=_now(),
            text=addition,
        )
    edit = dict(data.get("edit") or {})
    edit.update(
        {
            "count": int(edit.get("count") or 0) + 1,
            "edited_at": _now(),
            "before_chars": len(current),
            "after_chars": len(text),
            "ingest_after_edit": bool(ingest_now),
        }
    )
    _patch_section(file_id, "edit", **edit)
    logger.info(
        "用户改了正文：%s（%d → %d 字，随后入库=%s）",
        file_id,
        len(current),
        len(text),
        ingest_now,
    )

    # 3) 重新质检 + 重新给建议（走的是同一个质量门，编辑不能绕过它）
    settings = get_settings()
    # Phase 17：图片的正文是**人手写的一段描述**（通常几十到几百字）。按图片规则判质会因为
    # 「平均每页字符数 < 100」一直判 needs_ocr —— 那样用户写多少描述都出不来（真机死循环）。
    # 所以这里按纯文本判质：内容太少仍然 blocked（门没白设，还有「仍要入库」兜底）。
    route_for_check = "text" if image_job else str(data.get("route") or "text")
    outcome = _stage_quality(
        file_id,
        settings,
        text,
        route=route_for_check,
        pages=int(data.get("pages") or 1),
        allow_auto_ocr=False,
        # 用户是在改文字，不是在决定「要不要识别图片」：别再把他送回图片确认那一屏
        images_settled=True,
    )
    state_after = str(outcome.get("state") or "")
    ingested = state_after == STATE_DONE

    # 4) 「保存并入库」：只有质检通过、停在确认门上的才继续（等价于点「入库」）
    if ingest_now and state_after == STATE_REVIEW:
        outcome = _stage_ingest(file_id, settings, forced=False, confirmed=True)
        state_after = str(outcome.get("state") or "")
        ingested = state_after == STATE_DONE

    fresh = read_result(file_id) or {}
    reason = _first_reason(fresh)
    detail = str(fresh.get("detail") or "")
    delta = "%d → %d 字" % (len(current), len(text))
    # Phase 17：「写描述」说的是描述，别说成"改正文"（用户没看到正文长什么样）
    prefix = (
        "已保存描述（%d 字，正文共 %d 字）" % (len(addition), len(text))
        if caption
        else "已保存修改（%s）" % delta
    )
    if state_after == STATE_DONE:
        message = "%s，已提交知识库%s，正在构建索引" % (
            prefix,
            GATE_CONFIRMED_NOTE if ingest_now else "",
        )
    elif state_after == STATE_REVIEW:
        message = "%s，重新质检通过，仍在等你点「入库 / 跳过」" % prefix
    elif state_after == STATE_BLOCKED:
        message = "%s，但重新质检没过：%s（可继续改，或点「仍要入库」）" % (
            prefix,
            reason or "内容质量不达标",
        )
    elif state_after == STATE_NEEDS_OCR:
        message = "%s，但现在需要 OCR 识别：%s" % (prefix, detail or "正文过少")
    elif state_after == STATE_FAILED:
        message = "%s，但重新处理失败：%s" % (prefix, detail or "请点「重试」")
    else:
        message = "%s，当前状态：%s" % (
            prefix,
            STATE_LABELS.get(state_after, state_after or "未知"),
        )

    return {
        "ok": True,
        "http_status": 200,
        "action": "edit",
        "state": state_after,
        "chars": len(text),
        "edited": True,
        "ingested": ingested,
        "message": message,
        "status": status(file_id),
    }




