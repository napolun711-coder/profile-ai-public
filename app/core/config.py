"""应用配置读取（Phase 1：配置层，零新增依赖）。

设计要点：
1. 纯标准库实现，不引入 pydantic-settings / python-dotenv；
2. 优先级：系统环境变量 > .env 文件 > 代码默认值
   （与 LightRAG 的 load_dotenv(override=False) 语义一致）；
3. 所有字段默认值均与改造前的硬编码值相同，因此没有 .env 时行为完全一致；
4. .env 解析刻意保持简单：KEY=VALUE、忽略 # 注释、去成对引号、不做变量插值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

from app.core.paths import BASE_DIR, ENV_FILE
from app.core import model_policy

# 与 LightRAG config.py 的 max_upload_size 默认值对齐（100 MB）
DEFAULT_MAX_UPLOAD_SIZE: int = 104857600

DEFAULT_ALLOWED_EXTENSIONS: Tuple[str, ...] = (
    ".txt", ".md", ".pdf", ".doc", ".docx", ".csv",
    ".xlsx", ".xls", ".pptx", ".json", ".html",
    # Phase 8：图片（仅用于「OCR 解析」，OCR_MODE=off 时会被质量门拦截）
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
)

DEFAULT_CORS_ORIGINS: Tuple[str, ...] = (
    "http://127.0.0.1:8100",
    "http://localhost:8100",
)

# 「浏览器打不开」的主机名：LIGHTRAG_URL 用的这些地址只对**本机进程/容器**有意义，
# 不能直接交给浏览器（127.0.0.1 在手机上指的是手机自己；host.docker.internal 只有容器里认）。
# GET /graph 遇到这类主机名时，会改写成「浏览器访问本应用时用的那个主机名」+ LightRAG 的端口
# （实现见 app/clients/lightrag.py 的 public_base_url）。
BROWSER_UNREACHABLE_HOSTS: Tuple[str, ...] = (
    "127.0.0.1",
    "localhost",
    "0.0.0.0",
    "::1",
    "[::1]",
    "host.docker.internal",
)

# ---------- Phase 8：解析与质量门 ----------
# OCR 作业地址（官方 PaddleOCR-VL 服务；可换成千帆或自建服务，代码里不写死）
DEFAULT_OCR_BASE_URL: str = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
DEFAULT_OCR_MODEL: str = "PaddleOCR-VL-1.6"

# off = 不外发；manual = 用户点「OCR 解析」才外发；auto = 检测到需要即自动外发
OCR_MODES: Tuple[str, ...] = ("off", "manual", "auto")

# OCR 提供方（Phase 9）：
#   paddle = 云端 PaddleOCR-VL 作业接口（唯一会外发的提供方）
#   local  = 本机 PaddleOCR（不出网，需自行安装 paddleocr）
#   stub   = 离线替身（单测/演示用，不识别真实内容）
#   none   = 明确禁用（即使填了令牌也不外发）
OCR_PROVIDERS: Tuple[str, ...] = ("paddle", "local", "stub", "none")
DEFAULT_OCR_PROVIDER: str = "none"
DEFAULT_OCR_LANG: str = "ch"
OCR_DEVICES: Tuple[str, ...] = ("cpu", "gpu")

# 质量门预设（Phase 9）：standard 与 Phase 8 的默认阈值完全一致；
# custom = 逐项使用 QUALITY_* 自定义阈值
QUALITY_PROFILES: Tuple[str, ...] = ("standard", "strict", "loose", "custom")
DEFAULT_QUALITY_PROFILE: str = "standard"

# ---------- Phase 15：入库确认门（每个文件都要先确认一次才进知识库）----------
# always = 每个通过质检的文件都停下来等你点「入库」/「跳过」（默认，推荐）
# auto   = 智能建议判定「可以直接入库」时自动入库，有风险才停下（少打扰）
# off    = 不做确认，通过质检即入库（Phase 8 的老行为）
INGEST_CONFIRM_MODES: Tuple[str, ...] = ("always", "auto", "off")
DEFAULT_INGEST_CONFIRM: str = "always"
# 待确认作业的保留时长（小时）：超过它还没处理的，服务启动时自动归档（状态标记为已跳过，绝不入库）
DEFAULT_PENDING_REVIEW_TTL_HOURS: int = 24

# ---------- Phase 16：文件夹轮询自动入库（data\inbox）----------
# 丢进 data\inbox\ 的文件会被后台线程自动受理（解析 → 质检 → 按确认门入库）。
# 这是**又一个上传入口**，解析/质检/留档规则与网页上传完全一致；
# 不同点只有一处：`INBOX_CONFIRM` 可以给「文件夹来的文件」单独设确认门（默认 auto）。
DEFAULT_INBOX_ENABLED: bool = False
DEFAULT_INBOX_POLL_SECONDS: int = 10
# 文件要先「长稳」多久才受理：连续两轮大小不变 + 至少静置这么久（复制大文件要时间）
DEFAULT_INBOX_SETTLE_SECONDS: int = 5
# always = 每个文件都停下等你点「入库 / 跳过」；auto = 建议「可以直接入库」才自动放行（默认）；
# off = 通过质检即入库。与 INGEST_CONFIRM 取值相同，只是作用范围限于收件箱来的文件。
DEFAULT_INBOX_CONFIRM: str = "always"

# 允许做「整篇 OCR」的解析路由（PaddleOCR-VL 支持 PDF 与图片；其它格式请勿加入）
DEFAULT_OCR_ROUTES: Tuple[str, ...] = ("pdf", "image")

# 内嵌图片抽取上限（防止超大文档一次抽出几百张图占满内存 / 云端配额）
DEFAULT_PARSE_MAX_ASSETS: int = 50
DEFAULT_PARSE_MAX_ASSET_MB: int = 20

# 允许多少张「未识别图片」仍可入库：0 = 一张都不允许（默认，保守策略）
DEFAULT_QUALITY_MAX_UNOCR_IMAGES: int = 0


TRUE_VALUES = ("1", "true", "yes", "on")
FALSE_VALUES = ("0", "false", "no", "off")



def _strip_inline_comment(value: str) -> str:
    """去掉行尾注释；仅当 # 前是空白字符时才视为注释（避免破坏 URL）。"""
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1] in " \t":
            return value[:index].rstrip()
    return value


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析 .env 文件为字典；文件不存在或不可读时返回空字典。"""
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\x27\"":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _to_bool(value: str, default: bool) -> bool:
    text = (value or "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def _to_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_ocr_mode(value: str, default: str) -> str:
    """OCR 模式归一化：只接受 off / manual / auto，其他值回落到默认值。"""
    text = (value or "").strip().lower()
    return text if text in OCR_MODES else default


def _to_choice(value: str, default: str, allowed: Tuple[str, ...]) -> str:
    """枚举归一化：只接受 allowed 中的值（大小写不敏感），否则回落到默认值。"""
    text = (value or "").strip().lower()
    return text if text in allowed else default




def _to_list(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """运行期配置快照（不可变）。"""

    app_host: str = "127.0.0.1"
    # 阶段 0：局域网访问会话/记忆接口的口令。留空 = 自动生成并写入
    # data/chat_access_token.txt；只在 APP_HOST=0.0.0.0 时生效。
    chat_access_token: str = ""
    app_port: int = 8100

    # ---------- Batch 2：本地 AI 桥接（AHK → 本应用；纯工作流，不读写记忆） ----------
    # 默认 off；只做内存中转，不落盘、不记正文。TTL 覆盖“复制回复 → 回到网页”的时间。
    ai_bridge_enabled: bool = False
    ai_bridge_ttl_seconds: int = 300
    ai_bridge_max_chars: int = 65536
    # 收到回传文本后直接调用现有聊天入口发送；默认 off（只填入，等用户确认）。
    ai_bridge_auto_send: bool = False

    # ---------- Phase 10：关闭聊天窗口即停服 ----------
    # 前端在页面离开时只「预约」停止；任何一个页面报 keepalive 都会取消预约，
    # 因此刷新 / 跳设置页 / 多标签切换都不会误停。真正的判断与动刀都在后端。
    close_stops_services: bool = True
    close_stop_delay: int = 25             # 关窗口后等多少秒再停（给刷新留出时间）
    close_stop_busy_delay: int = 300       # 有入库作业在跑时改等这么久（别打断索引）
    # ---------- Phase 10.1：手机 / 局域网（权限只给一半） ----------
    # 手机页面用的是电脑的局域网 IP，不在「本机」白名单里：
    #   · lan_keepalive=true 时，手机页面能「续命」——把已预约的停止往后推，但制造不出停止；
    #   · 无论开关如何，手机都不能停服（停止接口只认本机 + 令牌）。
    lan_keepalive: bool = True
    close_stop_lan_delay: int = 180        # 手机续命后的宽限：切后台 / 锁屏要留余量

    lightrag_url: str = "http://127.0.0.1:9621"
    # 查询模式**已锁定为 "mix"**（见 app/clients/lightrag.py 的 QUERY_MODE），不再提供配置项：
    # 原 .env 的 LIGHTRAG_MODE=hybrid 与 LightRAG WebUI 检索面板的默认值（Mix）不一致，
    # 会出现"同一个知识库、两套答案"；现在两侧统一为 mix 且不可改。
    # 检索参数：**显式随请求下发**（对齐 WebUI 检索面板的默认值），不再依赖上游 .env 的历史值，
    # 换机器行为一致。留成配置项是为了给"定向设置"留口子，默认即推荐值。
    lightrag_top_k: int = 50
    lightrag_chunk_top_k: int = 30
    lightrag_max_entity_tokens: int = 8000
    lightrag_max_relation_tokens: int = 10000
    lightrag_max_total_tokens: int = 36000
    graph_max_nodes: int = 3500
    lightrag_timeout: int = 180
    lightrag_connect_timeout: int = 10
    lightrag_retries: int = 2
    lightrag_include_references: bool = False
    # 浏览器打开「知识图谱」时用的 LightRAG 地址（留空 = 按访问本应用的主机名自动推导；
    # 反代 / 特殊网络下可以显式指定，例如 https://rag.example.com）
    lightrag_public_url: str = ""

    # P2F：/chat 与 /chat/stream 的技能路由开关。off = 原路径直通上游（默认）；
    # on = 规则路由到 knowledge_qa / image_lookup，失败回退上游。改值需要重启；
    # 设置页入口在 P2G。
    skill_routing: str = "off"

    # ---------- P3B：回答生成方式（默认上游，零行为变化）----------
    # upstream = /chat 继续用 LightRAG 生成（默认，与改造前一致）；
    # local    = 用本机 Ollama 生成（P3A 链路），任何失败自动回退原路径。
    # 这是生产启用开关；LOCAL_LLM_ENABLED 仍然只是 P3A 脚本/测试的直调保险丝。
    generation_mode: str = "upstream"

    # ---------- M1：对话持久化 / 记忆系统 ----------
    # off（默认）时完全不创建/不写入数据库，聊天路径与改造前一致。
    # on 时 /chat 与 /chat/stream 会把会话和消息写入 data/memory/profile-ai.db。
    memory_enabled: bool = False
    # 数据库路径；留空 = data/memory/profile-ai.db。相对路径按项目根目录解析。
    memory_db_path: str = ""

    # ---------- M2：短期上下文 / Tier 1 快记忆 ----------
    # 是否把近期对话与长期记忆注入生成；off 时请求体与 M1/改造前一致。
    # 默认关闭：只开 MEMORY_ENABLED 时不得顺带注入记忆。
    memory_context_enabled: bool = False
    # Tier 1：用户稳定陈述/明确记忆的同步规则快路径；off 时只注入已有记忆。
    # 默认关闭：只开 MEMORY_ENABLED 时不得顺带写记忆。
    memory_fast_memory: bool = False
    # 长期记忆注入的 token 上限（所有记忆块合计）。
    memory_context_budget: int = 900
    # 近期对话注入条数与 token 上限。
    memory_recent_history_messages: int = 8
    memory_recent_history_tokens: int = 1200
    # 上游 LightRAG 是否接收 conversation_history/user_prompt（**分别控制**）：
    # auto = 探测 /openapi.json；on = 总是发送；off = 从不发送。
    # 阶段 0 起默认 off：内容不外发是默认值，需要时再显式打开。
    memory_upstream_history: str = "off"
    # 长期记忆（user_prompt）单独开关；与历史分开，避免"只想带历史"时被迫外发记忆。
    memory_upstream_memory: str = "off"
    # M4：跨会话历史召回（默认关闭，M4 再打开）。
    memory_recall_enabled: bool = False
    # P0-3B2b：有资料集的会话，聊天是否只使用本案已关联文档。
    # 默认 off：新能力先以关闭交付；打开后 bypass 生成只基于过滤后的 chunks。
    chat_topic_filter: bool = False
    # M4：跨会话历史摘录预算（与近期对话/长期记忆分开）。
    memory_recall_budget: int = 400
    memory_recall_max_items: int = 5
    # M3：异步自动提取（默认关闭，M3 再打开）。
    memory_auto_extract: bool = False
    # M3 任务队列：worker 是否随服务启动；真正的提取仍受 MEMORY_AUTO_EXTRACT 控制。
    # 默认关闭：只有显式打开提取时才启动 worker。
    memory_worker: bool = False
    # 每累计多少条未处理用户消息，立即入队（不再等去抖）。
    memory_extract_every_turns: int = 6
    # 单批最多读取多少条消息 / 多少输入 tokens。
    memory_extract_max_messages: int = 20
    memory_extract_max_input_tokens: int = 4000
    # 提取后端：auto/local/upstream/off；cloud 许可单独控制。
    memory_extract_backend: str = "auto"
    memory_cloud_extract: bool = False
    # 普通对话的去抖时间（秒）：到期后 worker 才处理。
    memory_extract_debounce_seconds: int = 300
    # M6：备份保留份数（默认 14；恢复前会自动再做一份 pre-restore 备份）。
    memory_backup_keep: int = 14

    # ---------- P3A：本地生成模型（Ollama，默认关闭）----------
    # off = 不调用本地模型；on = 允许本地链路（scripts/p3a_local_chain.py）调用。
    # P3B 的 local 模式不需要打开它（生产门面会构造受控的启用副本）；
    # 地址只允许回环（校验在 app/clients/llm.py）。
    llm_enabled: bool = False
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    # 本机 7B 在 CPU 上生成 200 token 约需 90 秒，120 秒会误杀正常回答。
    llm_timeout: int = 300
    llm_connect_timeout: int = 5
    # Ollama 默认上下文窗口可能只有 2048，装不下 ContextBuilder 的 4000 token 预算
    llm_num_ctx: int = 8192
    # 限制单次生成长度，避免模型一直写下去把请求拖到超时
    llm_num_predict: int = 512
    # P3C.2：默认仍是温度 0 + 固定种子（可复现）；调优时可单独放宽。
    llm_temperature: float = 0.0
    # P3C.2：让 Ollama 在两次提问之间保留模型加载（减少冷启动，仍是本机内存）。
    # 空字符串 = 不发送 keep_alive，由 Ollama 用自己的默认值。
    llm_keep_alive: str = "5m"
    # P3C.2：ContextBuilder 的上下文预算（tokens）；默认与 P3A 的 4000 一致。
    llm_context_budget: int = 4000
    # P3C.3：本机模式的首 token 保护。首字前超时（秒）则回退上游；
    # 已输出内容后不适用（仍按“报错不回退”处理）。
    llm_first_token_timeout: int = 120
    # P3C.3：同时进行的本机生成上限；超出的请求直接回退上游，避免 CPU 排队雪崩。
    llm_max_concurrency: int = 1

    max_upload_size: int = DEFAULT_MAX_UPLOAD_SIZE
    allowed_extensions: Tuple[str, ...] = DEFAULT_ALLOWED_EXTENSIONS

    # ---------- Phase 8：文档解析与质量门 ----------
    parse_max_chars: int = 2_000_000
    ingest_async: bool = True

    # Phase 18：兼容保留的老键 —— 质检是硬门（恒定运行），这个值**不再影响任何判定**，
    # 只用于 /api/settings 的 quality_effective.switch_ignored 与启动日志提示「已被忽略」。
    quality_enabled: bool = True
    quality_profile: str = DEFAULT_QUALITY_PROFILE
    quality_min_chars: int = 50
    quality_min_readable_ratio: float = 0.60
    quality_max_garbled_ratio: float = 0.05
    quality_max_repeat_ratio: float = 0.40
    quality_min_chars_per_page: int = 100
    quality_pass_score: int = 70
    quality_max_unocr_images: int = DEFAULT_QUALITY_MAX_UNOCR_IMAGES
    quality_ocr_routes: Tuple[str, ...] = DEFAULT_OCR_ROUTES

    # ---------- Phase 15：入库确认门（每个文件都要先确认一次才进知识库）----------
    # 通过质检后不再直接入库，而是停在 review 状态，等用户点「入库」/「跳过」；
    # 详见 docs\设计_本地解析与质量门.md 的「入库确认门」一节。
    ingest_confirm: str = DEFAULT_INGEST_CONFIRM
    pending_review_ttl_hours: int = DEFAULT_PENDING_REVIEW_TTL_HOURS

    # ---------- Phase 16：文件夹轮询自动入库（data\inbox）----------
    # 关闭后不再扫描收件箱（已丢进去的文件原样留着，不会丢；打开开关重启即继续）。
    inbox_enabled: bool = DEFAULT_INBOX_ENABLED
    inbox_poll_seconds: int = DEFAULT_INBOX_POLL_SECONDS
    inbox_settle_seconds: int = DEFAULT_INBOX_SETTLE_SECONDS
    # 只作用于「文件夹轮询」这一个入口的确认门：always / auto / off（默认 auto）
    inbox_confirm: str = DEFAULT_INBOX_CONFIRM

    # ---------- Phase 8/9：OCR（默认关闭，不外发任何文件）----------
    ocr_mode: str = "off"
    ocr_provider: str = DEFAULT_OCR_PROVIDER
    ocr_base_url: str = DEFAULT_OCR_BASE_URL
    ocr_token: str = ""
    ocr_model: str = DEFAULT_OCR_MODEL
    ocr_lang: str = DEFAULT_OCR_LANG
    ocr_poll_interval: int = 5
    ocr_timeout: int = 600
    ocr_connect_timeout: int = 10
    ocr_max_file_mb: int = 50
    # 是否把文档内嵌图片纳入 OCR（true = 抽取并等待用户确认；false = 忽略图片）
    ocr_image_ocr: bool = True
    # 本机 OCR（provider=local）用：模型目录与设备
    ocr_local_model_dir: str = ""
    ocr_device: str = "cpu"

    # ---------- Phase 9：内嵌图片抽取上限 ----------
    parse_max_assets: int = DEFAULT_PARSE_MAX_ASSETS
    parse_max_asset_mb: int = DEFAULT_PARSE_MAX_ASSET_MB

    qdrant_url: str = "http://localhost:6333"
    ollama_url: str = "http://localhost:11434"
    health_timeout: int = 3

    cors_origins: Tuple[str, ...] = DEFAULT_CORS_ORIGINS

    log_level: str = "INFO"

    env_file: str = ""
    env_loaded: bool = False

    @property
    def cors_allow_credentials(self) -> bool:
        """allow_origins 为 * 时浏览器不接受 credentials，需自动关闭。"""
        return "*" not in self.cors_origins

    @property
    def ocr_available(self) -> bool:
        """（兼容旧名）是否允许调用 OCR：等同于 ocr_enabled。"""
        return self.ocr_enabled

    @property
    def ocr_enabled(self) -> bool:
        """OCR 是否可用：模式非 off + 提供方是已知值且不是 none + 该提供方所需凭据齐备。"""
        if self.ocr_mode not in ("manual", "auto"):
            return False
        provider = (self.ocr_provider or "").strip().lower()
        if provider == "none" or provider not in OCR_PROVIDERS:
            return False
        if provider == "paddle":
            return bool(self.ocr_token.strip())
        return True  # local / stub：无需令牌（可用性在调用时再校验依赖）

    @property
    def ocr_external(self) -> bool:
        """启用后是否会向外部服务发送文件（用于界面上明确提示「会外发」）。"""
        return self.ocr_enabled and (self.ocr_provider or "").strip().lower() == "paddle"

    def to_log_dict(self) -> Dict[str, object]:
        """启动日志用（不含任何密钥）。"""
        return {
            "env_file": self.env_file,
            "env_loaded": self.env_loaded,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "lightrag_url": self.lightrag_url,
            "lightrag_timeout": self.lightrag_timeout,
            "lightrag_include_references": self.lightrag_include_references,
            "lightrag_public_url": self.lightrag_public_url,
            "skill_routing": self.skill_routing,
            "generation_mode": self.generation_mode,
            "memory_enabled": self.memory_enabled,
            "memory_db_path": self.memory_db_path or "data/memory/profile-ai.db",
            "memory_context_enabled": self.memory_context_enabled,
            "memory_fast_memory": self.memory_fast_memory,
            "memory_context_budget": self.memory_context_budget,
            "memory_recent_history_messages": self.memory_recent_history_messages,
            "memory_recent_history_tokens": self.memory_recent_history_tokens,
            "memory_upstream_history": self.memory_upstream_history,
            "memory_upstream_memory": self.memory_upstream_memory,
            "chat_access_token_set": bool(self.chat_access_token),
            "memory_recall_enabled": self.memory_recall_enabled,
            "chat_topic_filter": self.chat_topic_filter,
            "memory_recall_budget": self.memory_recall_budget,
            "memory_recall_max_items": self.memory_recall_max_items,
            "memory_auto_extract": self.memory_auto_extract,
            "memory_worker": self.memory_worker,
            "memory_extract_every_turns": self.memory_extract_every_turns,
            "memory_extract_max_messages": self.memory_extract_max_messages,
            "memory_extract_max_input_tokens": self.memory_extract_max_input_tokens,
            "memory_extract_backend": self.memory_extract_backend,
            "memory_cloud_extract": self.memory_cloud_extract,
            "memory_extract_debounce_seconds": self.memory_extract_debounce_seconds,
            "memory_backup_keep": self.memory_backup_keep,
            "llm_enabled": self.llm_enabled,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "llm_num_ctx": self.llm_num_ctx,
            "llm_num_predict": self.llm_num_predict,
            "llm_temperature": self.llm_temperature,
            "llm_keep_alive": self.llm_keep_alive,
            "llm_context_budget": self.llm_context_budget,
            "llm_first_token_timeout": self.llm_first_token_timeout,
            "llm_max_concurrency": self.llm_max_concurrency,
            "lightrag_query": {
                "mode": "mix（已锁定，见 clients/lightrag.QUERY_MODE）",
                "top_k": self.lightrag_top_k,
                "chunk_top_k": self.lightrag_chunk_top_k,
                "max_entity_tokens": self.lightrag_max_entity_tokens,
                "max_relation_tokens": self.lightrag_max_relation_tokens,
                "max_total_tokens": self.lightrag_max_total_tokens,
                "graph_max_nodes": self.graph_max_nodes,
            },
            "max_upload_size": self.max_upload_size,
            "cors_origins": list(self.cors_origins),
            "log_level": self.log_level,
            "quality_enabled": self.quality_enabled,
            "quality_profile": self.quality_profile,
            "ingest_async": self.ingest_async,
            "ingest_confirm": self.ingest_confirm,
            "pending_review_ttl_hours": self.pending_review_ttl_hours,
            # ---------- Phase 16：文件夹轮询只打配置，不打任何文件名 ----------
            "inbox_enabled": self.inbox_enabled,
            "inbox_poll_seconds": self.inbox_poll_seconds,
            "inbox_settle_seconds": self.inbox_settle_seconds,
            "inbox_confirm": self.inbox_confirm,
            "ocr_mode": self.ocr_mode,
            "ocr_provider": self.ocr_provider,
            "ocr_base_url": self.ocr_base_url,
            "ocr_model": self.ocr_model,
            "ocr_token_set": bool(self.ocr_token.strip()),
            "ocr_enabled": self.ocr_enabled,
            "ocr_external": self.ocr_external,
            "ocr_image_ocr": self.ocr_image_ocr,
        }



def _build_settings() -> Settings:
    file_values = parse_env_file(ENV_FILE)
    defaults = Settings()

    def read(key: str, default: str = "") -> str:
        if key in os.environ:
            return os.environ[key]
        if key in file_values:
            return file_values[key]
        return default

    return Settings(
        app_host=read("APP_HOST", read("HOST", defaults.app_host)),
        app_port=_to_int(read("APP_PORT", read("PORT", "")), defaults.app_port),
        ai_bridge_enabled=_to_bool(read("AI_BRIDGE_ENABLED"), defaults.ai_bridge_enabled),
        ai_bridge_ttl_seconds=max(
            5, _to_int(read("AI_BRIDGE_TTL_SECONDS"), defaults.ai_bridge_ttl_seconds)
        ),
        ai_bridge_max_chars=max(
            256, _to_int(read("AI_BRIDGE_MAX_CHARS"), defaults.ai_bridge_max_chars)
        ),
        ai_bridge_auto_send=_to_bool(
            read("AI_BRIDGE_AUTO_SEND"), defaults.ai_bridge_auto_send
        ),
        close_stops_services=_to_bool(read("CLOSE_STOPS_SERVICES"), defaults.close_stops_services),
        close_stop_delay=max(5, _to_int(read("CLOSE_STOP_DELAY"), defaults.close_stop_delay)),
        close_stop_busy_delay=max(
            30, _to_int(read("CLOSE_STOP_BUSY_DELAY"), defaults.close_stop_busy_delay)
        ),
        lan_keepalive=_to_bool(read("LAN_KEEPALIVE"), defaults.lan_keepalive),
        close_stop_lan_delay=max(
            30, _to_int(read("CLOSE_STOP_LAN_DELAY"), defaults.close_stop_lan_delay)
        ),
        lightrag_url=read("LIGHTRAG_URL", defaults.lightrag_url),
        lightrag_top_k=_to_int(read("LIGHTRAG_TOP_K"), defaults.lightrag_top_k),
        lightrag_chunk_top_k=_to_int(read("LIGHTRAG_CHUNK_TOP_K"), defaults.lightrag_chunk_top_k),
        lightrag_max_entity_tokens=_to_int(
            read("LIGHTRAG_MAX_ENTITY_TOKENS"), defaults.lightrag_max_entity_tokens
        ),
        lightrag_max_relation_tokens=_to_int(
            read("LIGHTRAG_MAX_RELATION_TOKENS"), defaults.lightrag_max_relation_tokens
        ),
        lightrag_max_total_tokens=_to_int(
            read("LIGHTRAG_MAX_TOTAL_TOKENS"), defaults.lightrag_max_total_tokens
        ),
        graph_max_nodes=max(
            50,
            min(
                10000,
                _to_int(read("GRAPH_MAX_NODES"), defaults.graph_max_nodes),
            ),
        ),
        lightrag_timeout=_to_int(read("LIGHTRAG_TIMEOUT"), defaults.lightrag_timeout),
        lightrag_connect_timeout=_to_int(read("LIGHTRAG_CONNECT_TIMEOUT"), defaults.lightrag_connect_timeout),
        lightrag_retries=_to_int(read("LIGHTRAG_RETRIES"), defaults.lightrag_retries),
        lightrag_include_references=_to_bool(
            read("LIGHTRAG_INCLUDE_REFERENCES"), defaults.lightrag_include_references
        ),
        lightrag_public_url=read(
            "LIGHTRAG_PUBLIC_URL", defaults.lightrag_public_url
        ).rstrip("/"),
        # P2F：非法值一律回落 off，避免拼写错误把 /chat 切到未验证路径。
        skill_routing=_to_choice(
            read("SKILL_ROUTING"), defaults.skill_routing, ("off", "on")
        ),
        # 本机 LLM 已全局禁用；无论 .env 写什么，生产配置固定走上游。
        generation_mode=model_policy.GENERATION_MODE,
        memory_enabled=_to_bool(read("MEMORY_ENABLED"), defaults.memory_enabled),
        memory_db_path=read("MEMORY_DB_PATH", defaults.memory_db_path).strip(),
        # M2：上下文注入 / Tier 1
        memory_context_enabled=_to_bool(
            read("MEMORY_CONTEXT_ENABLED"), defaults.memory_context_enabled
        ),
        memory_fast_memory=_to_bool(
            read("MEMORY_FAST_MEMORY"), defaults.memory_fast_memory
        ),
        memory_context_budget=max(
            100,
            min(
                8000,
                _to_int(read("MEMORY_CONTEXT_BUDGET"), defaults.memory_context_budget),
            ),
        ),
        memory_recent_history_messages=max(
            0,
            min(
                40,
                _to_int(
                    read("MEMORY_RECENT_HISTORY_MESSAGES"),
                    defaults.memory_recent_history_messages,
                ),
            ),
        ),
        memory_recent_history_tokens=max(
            0,
            min(
                16000,
                _to_int(
                    read("MEMORY_RECENT_HISTORY_TOKENS"),
                    defaults.memory_recent_history_tokens,
                ),
            ),
        ),
        memory_upstream_history=_to_choice(
            read("MEMORY_UPSTREAM_HISTORY"),
            defaults.memory_upstream_history,
            ("auto", "on", "off"),
        ),
        memory_upstream_memory=_to_choice(
            read("MEMORY_UPSTREAM_MEMORY"),
            defaults.memory_upstream_memory,
            ("auto", "on", "off"),
        ),
        chat_access_token=read("CHAT_ACCESS_TOKEN", defaults.chat_access_token),
        memory_recall_enabled=_to_bool(
            read("MEMORY_RECALL_ENABLED"), defaults.memory_recall_enabled
        ),
        chat_topic_filter=_to_bool(
            read("CHAT_TOPIC_FILTER"), defaults.chat_topic_filter
        ),
        memory_recall_budget=max(
            80,
            min(
                2000,
                _to_int(
                    read("MEMORY_RECALL_BUDGET"), defaults.memory_recall_budget
                ),
            ),
        ),
        memory_recall_max_items=max(
            1,
            min(
                20,
                _to_int(
                    read("MEMORY_RECALL_MAX_ITEMS"),
                    defaults.memory_recall_max_items,
                ),
            ),
        ),
        memory_auto_extract=_to_bool(
            read("MEMORY_AUTO_EXTRACT"), defaults.memory_auto_extract
        ),
        memory_worker=_to_bool(read("MEMORY_WORKER"), defaults.memory_worker),
        memory_extract_every_turns=max(
            1,
            min(
                100,
                _to_int(
                    read("MEMORY_EXTRACT_EVERY_TURNS"),
                    defaults.memory_extract_every_turns,
                ),
            ),
        ),
        memory_extract_max_messages=max(
            1,
            min(
                200,
                _to_int(
                    read("MEMORY_EXTRACT_MAX_MESSAGES"),
                    defaults.memory_extract_max_messages,
                ),
            ),
        ),
        memory_extract_max_input_tokens=max(
            200,
            min(
                32000,
                _to_int(
                    read("MEMORY_EXTRACT_MAX_INPUT_TOKENS"),
                    defaults.memory_extract_max_input_tokens,
                ),
            ),
        ),
        memory_extract_backend=_to_choice(
            read("MEMORY_EXTRACT_BACKEND"),
            defaults.memory_extract_backend,
            ("auto", "local", "upstream", "off"),
        ),
        memory_cloud_extract=_to_bool(
            read("MEMORY_CLOUD_EXTRACT"), defaults.memory_cloud_extract
        ),
        memory_extract_debounce_seconds=max(
            0,
            min(
                86400,
                _to_int(
                    read("MEMORY_EXTRACT_DEBOUNCE_SECONDS"),
                    defaults.memory_extract_debounce_seconds,
                ),
            ),
        ),
        memory_backup_keep=max(
            1,
            min(
                90,
                _to_int(
                    read("MEMORY_BACKUP_KEEP"),
                    defaults.memory_backup_keep,
                ),
            ),
        ),
        # P3A：本地生成模型（默认关闭）。地址合法性在客户端校验，这里只读原值。
        # 用 LOCAL_LLM_* 前缀，避免和设置页已有的 llm 组（LightRAG 引擎配置的
        # LLM_MODEL / LLM_BINDING_HOST 等注入键）撞名。
        # 本机 Ollama 只允许 bge-m3 嵌入，不参与任何 LLM 生成。
        llm_enabled=False,
        llm_base_url=read("LOCAL_LLM_BASE_URL", defaults.llm_base_url).rstrip("/"),
        llm_model=read("LOCAL_LLM_MODEL", defaults.llm_model),
        llm_timeout=max(5, _to_int(read("LOCAL_LLM_TIMEOUT"), defaults.llm_timeout)),
        llm_connect_timeout=max(
            1, _to_int(read("LOCAL_LLM_CONNECT_TIMEOUT"), defaults.llm_connect_timeout)
        ),
        llm_num_ctx=max(
            512, min(131072, _to_int(read("LOCAL_LLM_NUM_CTX"), defaults.llm_num_ctx))
        ),
        llm_num_predict=max(
            1, min(8192, _to_int(read("LOCAL_LLM_NUM_PREDICT"), defaults.llm_num_predict))
        ),
        llm_temperature=max(
            0.0,
            min(
                2.0,
                _to_float(
                    read("LOCAL_LLM_TEMPERATURE"), defaults.llm_temperature
                ),
            ),
        ),
        llm_keep_alive=read(
            "LOCAL_LLM_KEEP_ALIVE", defaults.llm_keep_alive
        ).strip(),
        llm_context_budget=max(
            500,
            min(
                32000,
                _to_int(
                    read("LOCAL_LLM_CONTEXT_BUDGET"),
                    defaults.llm_context_budget,
                ),
            ),
        ),
        llm_first_token_timeout=max(
            5,
            min(
                600,
                _to_int(
                    read("LOCAL_LLM_FIRST_TOKEN_TIMEOUT"),
                    defaults.llm_first_token_timeout,
                ),
            ),
        ),
        llm_max_concurrency=max(
            1,
            min(
                8,
                _to_int(
                    read("LOCAL_LLM_MAX_CONCURRENCY"),
                    defaults.llm_max_concurrency,
                ),
            ),
        ),
        max_upload_size=_to_int(read("MAX_UPLOAD_SIZE"), defaults.max_upload_size),
        allowed_extensions=tuple(
            _to_list(read("ALLOWED_EXTENSIONS", ",".join(DEFAULT_ALLOWED_EXTENSIONS)))
        ),
        parse_max_chars=_to_int(read("PARSE_MAX_CHARS"), defaults.parse_max_chars),
        parse_max_assets=_to_int(read("PARSE_MAX_ASSETS"), defaults.parse_max_assets),
        parse_max_asset_mb=_to_int(read("PARSE_MAX_ASSET_MB"), defaults.parse_max_asset_mb),
        ingest_async=_to_bool(read("INGEST_ASYNC"), defaults.ingest_async),
        # Phase 18：照读原值（只为了如实告诉用户"你在 .env 里关过、但它被忽略了"），
        # 下游一律按"质检开启"处理 —— quality.py 已经没有任何总开关。
        quality_enabled=_to_bool(read("QUALITY_ENABLED"), defaults.quality_enabled),
        quality_profile=_to_choice(
            read("QUALITY_PROFILE"), defaults.quality_profile, QUALITY_PROFILES
        ),
        quality_min_chars=_to_int(read("QUALITY_MIN_CHARS"), defaults.quality_min_chars),
        quality_min_readable_ratio=_to_float(
            read("QUALITY_MIN_READABLE_RATIO"), defaults.quality_min_readable_ratio
        ),
        quality_max_garbled_ratio=_to_float(
            read("QUALITY_MAX_GARBLED_RATIO"), defaults.quality_max_garbled_ratio
        ),
        quality_max_repeat_ratio=_to_float(
            read("QUALITY_MAX_REPEAT_RATIO"), defaults.quality_max_repeat_ratio
        ),
        quality_min_chars_per_page=_to_int(
            read("QUALITY_MIN_CHARS_PER_PAGE"), defaults.quality_min_chars_per_page
        ),
        quality_pass_score=_to_int(read("QUALITY_PASS_SCORE"), defaults.quality_pass_score),
        quality_max_unocr_images=_to_int(
            read("QUALITY_MAX_UNOCR_IMAGES"), defaults.quality_max_unocr_images
        ),
        quality_ocr_routes=tuple(
            _to_list(read("QUALITY_OCR_ROUTES", ",".join(DEFAULT_OCR_ROUTES)))
        ),
        # Phase 15：入库确认门（always / auto / off）
        ingest_confirm=_to_choice(
            read("INGEST_CONFIRM"), defaults.ingest_confirm, INGEST_CONFIRM_MODES
        ),
        pending_review_ttl_hours=max(
            0, _to_int(read("PENDING_REVIEW_TTL_HOURS"), defaults.pending_review_ttl_hours)
        ),
        # ---------- Phase 16：文件夹轮询自动入库 ----------
        inbox_enabled=_to_bool(read("INBOX_ENABLED"), defaults.inbox_enabled),
        inbox_poll_seconds=max(
            3, min(600, _to_int(read("INBOX_POLL_SECONDS"), defaults.inbox_poll_seconds))
        ),
        inbox_settle_seconds=max(
            0, min(600, _to_int(read("INBOX_SETTLE_SECONDS"), defaults.inbox_settle_seconds))
        ),
        inbox_confirm=_to_choice(
            read("INBOX_CONFIRM"), defaults.inbox_confirm, INGEST_CONFIRM_MODES
        ),
        ocr_mode=_to_ocr_mode(read("OCR_MODE"), defaults.ocr_mode),
        ocr_provider=_to_choice(
            read("OCR_PROVIDER"), defaults.ocr_provider, OCR_PROVIDERS
        ),
        ocr_base_url=read("OCR_BASE_URL", defaults.ocr_base_url).rstrip("/"),
        ocr_token=read("OCR_TOKEN", defaults.ocr_token),
        ocr_model=read("OCR_MODEL", defaults.ocr_model),
        ocr_lang=read("OCR_LANG", defaults.ocr_lang),
        ocr_poll_interval=max(1, _to_int(read("OCR_POLL_INTERVAL"), defaults.ocr_poll_interval)),
        ocr_timeout=_to_int(read("OCR_TIMEOUT"), defaults.ocr_timeout),
        ocr_connect_timeout=_to_int(
            read("OCR_CONNECT_TIMEOUT"), defaults.ocr_connect_timeout
        ),
        ocr_max_file_mb=_to_int(read("OCR_MAX_FILE_MB"), defaults.ocr_max_file_mb),
        ocr_image_ocr=_to_bool(read("OCR_IMAGE_OCR"), defaults.ocr_image_ocr),
        ocr_local_model_dir=read("OCR_LOCAL_MODEL_DIR", defaults.ocr_local_model_dir),
        ocr_device=_to_choice(read("OCR_DEVICE"), defaults.ocr_device, OCR_DEVICES),

        qdrant_url=read("QDRANT_URL", defaults.qdrant_url),
        ollama_url=read("OLLAMA_URL", defaults.ollama_url),
        health_timeout=_to_int(read("HEALTH_TIMEOUT"), defaults.health_timeout),
        cors_origins=tuple(
            _to_list(read("CORS_ORIGINS", ",".join(DEFAULT_CORS_ORIGINS)))
        ),
        log_level=read("LOG_LEVEL", defaults.log_level).upper(),
        env_file=str(ENV_FILE),
        env_loaded=bool(file_values),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例（首次调用时读取 .env 与系统环境变量）。"""
    return _build_settings()


def reload_settings() -> Settings:
    """清缓存并重新读取（测试用）。"""
    get_settings.cache_clear()
    return get_settings()
