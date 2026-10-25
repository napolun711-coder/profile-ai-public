"""用户可改配置（Phase 4.2）。

单一配置源：config/user-settings.json（项目内、不入库）。
- 网页设置页（app/static/settings.html）通过 app/routers/settings.py 读写本文件；
- 启动器 scripts/start.py 读取本文件，把值作为环境变量注入 LightRAG 与应用子进程
  （LightRAG 使用 load_dotenv(override=False)，环境变量优先，因此无需改动它的 .env）。

两条约定：
1. 普通字段留空 = 不注入该项（保持 LightRAG 自身 .env 的取值）；
2. API Key 留空 = 保持现有值不变（避免误清空）。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.core import model_policy
from app.core.paths import BASE_DIR

CONFIG_DIR = BASE_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "user-settings.json"
EXAMPLE_FILE = CONFIG_DIR / "user-settings.example.json"
BACKUP_FILE = CONFIG_DIR / "user-settings.json.bak"

VERSION = 1


DEFAULTS: Dict[str, Any] = {
    "version": VERSION,
    "updated_at": "",
    "llm": {
        "binding": model_policy.DEEPSEEK_BINDING,
        "model": model_policy.DEEPSEEK_MODEL,
        "host": model_policy.DEEPSEEK_BASE_URL,
        "api_key": "",
    },
    "embedding": {
        "binding": model_policy.EMBEDDING_BINDING,
        "model": model_policy.EMBEDDING_MODEL,
        "host": model_policy.EMBEDDING_BASE_URL,
        "api_key": "",
        "dim": "",
        "send_dim": "",
    },
    "rerank": {"binding": "", "model": "", "host": "", "api_key": ""},
    "vector_store": {"type": "", "qdrant_url": ""},
    "graph": {"working_dir": "", "input_dir": ""},
    "paths": {"lightrag_home": ""},
    # P2G：问答技能路由（留空 = 跟随 .env / 代码默认值 off；可填 off / on）
    "app": {
        "max_upload_size": "",
        "allowed_extensions": "",
        "log_level": "",
        "skill_routing": "",
        # 本机 LLM 已全局禁用，固定走 LightRAG 上游（其 LLM 固定 DeepSeek）。
        "generation_mode": model_policy.GENERATION_MODE,
    },
    # Phase 15：入库确认门（每个文件都要先确认一次才进知识库）
    # confirm 留空 = 跟随 .env / 代码默认值（always）；可填 always / auto / off
    # ttl_hours 留空 = 跟随 .env（默认 24 小时）；0 = 不自动归档待确认的作业
    "ingest": {"confirm": "", "ttl_hours": ""},
    # Phase 16：文件夹轮询自动入库（把文件丢进 data/inbox/ 就自动受理）
    # enabled 留空 = 跟随 .env / 默认值（true）；poll_seconds 留空 = 默认 10 秒；
    # confirm 留空 = 跟随 .env / 默认值（auto，只作用于文件夹来的文件）
    "inbox": {"enabled": "", "poll_seconds": "", "confirm": ""},
    # Phase 9：质量门与 OCR 的网页可配置项（留空 = 不注入，保持 .env / 默认值）
    # Phase 18：质检是硬门 —— "enabled" 只为兼容旧配置文件而保留，设置页不再给它入口，
    # 填了也不会改变行为（QUALITY_ENABLED 会被忽略并记一条启动日志）。
    "quality": {
        "profile": "",
        "enabled": "",
        "min_chars": "",
        "min_readable_ratio": "",
        "max_garbled_ratio": "",
        "max_repeat_ratio": "",
        "min_chars_per_page": "",
        "pass_score": "",
        "max_unocr_images": "",
    },
    "ocr": {
        "mode": "",
        "provider": "",
        "image_ocr": "",
        "lang": "",
        "device": "",
        "base_url": "",
        "model": "",
        "token": "",
        "max_file_mb": "",
        "local_model_dir": "",
    },
}

# 配置项 -> LightRAG 环境变量名
KEY_ENV_MAP: Dict[Tuple[str, str], str] = {
    ("llm", "binding"): "LLM_BINDING",
    ("llm", "model"): "LLM_MODEL",
    ("llm", "host"): "LLM_BINDING_HOST",
    ("llm", "api_key"): "LLM_BINDING_API_KEY",
    ("embedding", "binding"): "EMBEDDING_BINDING",
    ("embedding", "model"): "EMBEDDING_MODEL",
    ("embedding", "host"): "EMBEDDING_BINDING_HOST",
    ("embedding", "api_key"): "EMBEDDING_BINDING_API_KEY",
    ("embedding", "dim"): "EMBEDDING_DIM",
    ("embedding", "send_dim"): "EMBEDDING_SEND_DIM",
    ("rerank", "binding"): "RERANK_BINDING",
    ("rerank", "model"): "RERANK_MODEL",
    ("rerank", "host"): "RERANK_BINDING_HOST",
    ("rerank", "api_key"): "RERANK_BINDING_API_KEY",
    ("graph", "working_dir"): "WORKING_DIR",
    ("graph", "input_dir"): "INPUT_DIR",
}

# 本应用自身的配置项 -> 环境变量名（由启动器注入本应用子进程）
APP_ENV_MAP: Dict[Tuple[str, str], str] = {
    ("app", "max_upload_size"): "MAX_UPLOAD_SIZE",
    ("app", "allowed_extensions"): "ALLOWED_EXTENSIONS",
    ("app", "log_level"): "LOG_LEVEL",
    # P2G：/chat 技能路由开关（保存后重启生效；默认空 = 不注入 = 代码默认 off）
    ("app", "skill_routing"): "SKILL_ROUTING",
    # P3B：回答生成方式（保存后重启生效；默认空 = 不注入 = 代码默认 upstream）
    ("app", "generation_mode"): "GENERATION_MODE",
    # Phase 9：质量门（Phase 18 起 enabled 已失效，仅为兼容旧配置文件保留映射）
    ("quality", "profile"): "QUALITY_PROFILE",
    ("quality", "enabled"): "QUALITY_ENABLED",
    ("quality", "min_chars"): "QUALITY_MIN_CHARS",
    ("quality", "min_readable_ratio"): "QUALITY_MIN_READABLE_RATIO",
    ("quality", "max_garbled_ratio"): "QUALITY_MAX_GARBLED_RATIO",
    ("quality", "max_repeat_ratio"): "QUALITY_MAX_REPEAT_RATIO",
    ("quality", "min_chars_per_page"): "QUALITY_MIN_CHARS_PER_PAGE",
    ("quality", "pass_score"): "QUALITY_PASS_SCORE",
    ("quality", "max_unocr_images"): "QUALITY_MAX_UNOCR_IMAGES",
    # Phase 15：入库确认门（本应用自身的配置，见 APP_ENV_MAP 底部说明）
    ("ingest", "confirm"): "INGEST_CONFIRM",
    ("ingest", "ttl_hours"): "PENDING_REVIEW_TTL_HOURS",
    # Phase 16：文件夹轮询（本应用自身的配置）
    ("inbox", "enabled"): "INBOX_ENABLED",
    ("inbox", "poll_seconds"): "INBOX_POLL_SECONDS",
    ("inbox", "confirm"): "INBOX_CONFIRM",
    # Phase 9：OCR
    ("ocr", "mode"): "OCR_MODE",
    ("ocr", "provider"): "OCR_PROVIDER",
    ("ocr", "image_ocr"): "OCR_IMAGE_OCR",
    ("ocr", "lang"): "OCR_LANG",
    ("ocr", "device"): "OCR_DEVICE",
    ("ocr", "base_url"): "OCR_BASE_URL",
    ("ocr", "model"): "OCR_MODEL",
    ("ocr", "token"): "OCR_TOKEN",
    ("ocr", "max_file_mb"): "OCR_MAX_FILE_MB",
    ("ocr", "local_model_dir"): "OCR_LOCAL_MODEL_DIR",
}

# 枚举型配置的合法取值（留空 = 跟随 .env）
QUALITY_PROFILE_VALUES = ("standard", "strict", "loose", "custom")
OCR_MODE_VALUES = ("off", "manual", "auto")
# Phase 15：入库确认门（留空 = 跟随 .env / 代码默认值 always）
INGEST_CONFIRM_VALUES = ("always", "auto", "off")
# Phase 16：文件夹轮询的确认门（取值同上，作用范围只限收件箱来的文件；默认 auto）
INBOX_CONFIRM_VALUES = ("always", "auto", "off")
OCR_PROVIDER_VALUES = ("paddle", "local", "stub", "none")
OCR_DEVICE_VALUES = ("cpu", "gpu")
# P2G：问答技能路由（与 app/core/config.py 的 SKILL_ROUTING 取值一致）
SKILL_ROUTING_VALUES = ("off", "on")
# P3B：回答生成方式（与 app/core/config.py 的 GENERATION_MODE 取值一致）
GENERATION_MODE_VALUES = ("upstream",)
TRUTHY_VALUES = ("true", "false")   # 三态："" / "true" / "false"


# 数值型配置（必须是非负数值）
NUMERIC_KEYS: Tuple[Tuple[str, str], ...] = (
    ("quality", "min_chars"),
    ("quality", "min_chars_per_page"),
    ("quality", "pass_score"),
    ("quality", "max_unocr_images"),
    ("quality", "min_readable_ratio"),
    ("quality", "max_garbled_ratio"),
    ("quality", "max_repeat_ratio"),
    ("ocr", "max_file_mb"),
    # Phase 15：待确认作业保留时长（小时）
    ("ingest", "ttl_hours"),
    # Phase 16：文件夹轮询间隔（秒）
    ("inbox", "poll_seconds"),
)

VECTOR_STORAGE_ENV = "LIGHTRAG_VECTOR_STORAGE"
QDRANT_URL_ENV = "QDRANT_URL"
LIGHTRAG_HOME_ENV = "LIGHTRAG_HOME"

VECTOR_STORAGE_VALUES = {
    "qdrant": "QdrantVectorDBStorage",
    "local": "NanoVectorDBStorage",
}

SECRET_KEYS: Tuple[Tuple[str, str], ...] = (
    ("llm", "api_key"),
    ("embedding", "api_key"),
    ("rerank", "api_key"),
    ("ocr", "token"),
)

# 错误提示用的中文名（只列需要提示的项）
ENV_LABELS: Dict[Tuple[str, str], str] = {
    ("quality", "min_chars"): "最少有效字符数",
    ("quality", "min_chars_per_page"): "每页字符数下限",
    ("quality", "pass_score"): "综合质量分及格线",
    ("quality", "max_unocr_images"): "未识别图片容忍数",
    ("quality", "min_readable_ratio"): "可读字符占比下限",
    ("quality", "max_garbled_ratio"): "乱码比例上限",
    ("quality", "max_repeat_ratio"): "重复行比例上限",
    ("quality", "enabled"): "质量门开关（Phase 18 起已失效）",
    ("ocr", "max_file_mb"): "OCR 单文件上限",
    ("ocr", "image_ocr"): "内嵌图片纳入 OCR",
    ("ingest", "confirm"): "入库确认方式",
    ("ingest", "ttl_hours"): "待确认作业保留时长（小时）",
    ("inbox", "enabled"): "文件夹轮询开关",
    ("inbox", "poll_seconds"): "文件夹轮询间隔（秒）",
    ("inbox", "confirm"): "文件夹入库确认方式",
}

ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    # 以 base 为骨架，用 override 里的值覆盖（仅递归字典）
    result: Dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, dict):
            incoming = override.get(key, {}) if isinstance(override, dict) else {}
            result[key] = _deep_merge(value, incoming if isinstance(incoming, dict) else {})
        else:
            result[key] = override.get(key, value) if isinstance(override, dict) else value
    return result


def _finalize(data: Dict[str, Any]) -> Dict[str, Any]:
    # 兼容迁移：旧版本把 send_dim 默认写成了布尔值，布尔一律视为“未设置”（不注入）
    section = data.get("embedding")
    if isinstance(section, dict) and isinstance(section.get("send_dim"), bool):
        section["send_dim"] = ""
    for key, value in (
        ("binding", model_policy.DEEPSEEK_BINDING),
        ("model", model_policy.DEEPSEEK_MODEL),
        ("host", model_policy.DEEPSEEK_BASE_URL),
    ):
        data.setdefault("llm", {})[key] = value
    for key, value in (
        ("binding", model_policy.EMBEDDING_BINDING),
        ("model", model_policy.EMBEDDING_MODEL),
        ("host", model_policy.EMBEDDING_BASE_URL),
        ("api_key", ""),
    ):
        data.setdefault("embedding", {})[key] = value
    data.setdefault("app", {})["generation_mode"] = model_policy.GENERATION_MODE
    return data


def load() -> Dict[str, Any]:
    # 读取用户配置；文件不存在或损坏时返回默认值（行为与改造前一致）
    if not SETTINGS_FILE.is_file():
        return _finalize(_deep_merge(DEFAULTS, {}))
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _finalize(_deep_merge(DEFAULTS, {}))
    if not isinstance(raw, dict):
        return _finalize(_deep_merge(DEFAULTS, {}))
    return _finalize(_deep_merge(DEFAULTS, raw))


def snapshot_for_restart_check(data: Dict[str, Any]) -> str:
    """把配置压成可比较的字符串（忽略 updated_at / version 这类每次都变的元数据）。"""
    payload = json.loads(json.dumps(data, ensure_ascii=False))
    payload.pop("updated_at", None)
    payload.pop("version", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# 本次进程启动时的配置快照（Phase 9.2）：设置页据此说明「改动还没重启生效」
PROCESS_SETTINGS_SNAPSHOT = snapshot_for_restart_check(load())


def restart_needed(data: Dict[str, Any] | None = None) -> bool:
    """配置文件内容是否与本次进程启动时不同（不同 = 需要重启才生效）。"""
    current = load() if data is None else data
    return snapshot_for_restart_check(current) != PROCESS_SETTINGS_SNAPSHOT


def mask(data: Dict[str, Any]) -> Dict[str, Any]:
    # 返回可安全展示给前端的副本：密钥只保留尾部 4 位
    result = json.loads(json.dumps(data, ensure_ascii=False))
    for group, key in SECRET_KEYS:
        section = result.get(group)
        if isinstance(section, dict):
            value = str(section.get(key) or "")
            section[key] = ("******" + value[-4:]) if len(value) >= 8 else ("已配置" if value else "")
    return result

def apply_secret_clears(data: Dict[str, Any], clear: Any) -> Dict[str, Any]:
    """按页面提交的 clear_secrets 把指定密钥置空。

    密钥留空保存 = 保持原值，所以「清除」不能靠提交空字符串表达；
    页面改为单独列出一组 [group, key]，合并完成后再把它们清掉。
    只接受 SECRET_KEYS 里的项，其余（拼错、普通字段、非数组）一律忽略。
    """
    result = json.loads(json.dumps(data, ensure_ascii=False))
    if not isinstance(clear, list):
        return result
    for item in clear:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        pair = (str(item[0]), str(item[1]))
        if pair not in SECRET_KEYS:
            continue
        section = result.get(pair[0])
        if isinstance(section, dict):
            section[pair[1]] = ""
    return result


def _normalize_scalar(value: Any) -> str:
    # 统一成字符串（布尔转 true/false，None 转空串）
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def validate_and_merge(current: Dict[str, Any], incoming: Dict[str, Any]):
    # 校验并合并前端提交的配置；密钥留空表示保持原值
    errors: List[str] = []
    merged: Dict[str, Any] = {}
    for group, section in DEFAULTS.items():
        if not isinstance(section, dict):
            merged[group] = current.get(group, section)
            continue
        incoming_section = incoming.get(group) if isinstance(incoming, dict) else None
        if not isinstance(incoming_section, dict):
            incoming_section = {}
        merged_section: Dict[str, Any] = {}
        for key, default_value in section.items():
            raw = incoming_section.get(key, default_value)
            if isinstance(default_value, bool):
                merged_section[key] = bool(raw)
                continue
            text = _normalize_scalar(raw)
            if (group, key) in SECRET_KEYS and text == "":
                text = str(current.get(group, {}).get(key) or "")
            if (group, key) == ("app", "max_upload_size") and text and not text.isdigit():
                errors.append("上传大小上限必须是数字（单位字节）")
                text = ""
            if (group, key) == ("embedding", "dim") and text and not text.isdigit():
                errors.append("向量维度必须是数字")
                text = ""
            if (group, key) == ("app", "log_level") and text:
                upper = text.upper()
                if upper not in ALLOWED_LOG_LEVELS:
                    errors.append("日志级别只能是 DEBUG / INFO / WARNING / ERROR")
                    upper = ""
                text = upper
            if (group, key) == ("app", "skill_routing") and text:
                # P2G：拼写错误不能把 /chat 悄悄切到未验证路径；非法值让保存失败。
                lowered = text.lower()
                if lowered not in SKILL_ROUTING_VALUES:
                    errors.append("问答技能路由只能是 off / on（留空 = 跟随 .env / 默认关闭）")
                    lowered = ""
                text = lowered
            if (group, key) == ("app", "generation_mode") and text:
                lowered = text.lower()
                if lowered not in GENERATION_MODE_VALUES:
                    errors.append(
                        "本机 LLM 已全局禁用；回答生成方式只能是 upstream"
                    )
                    lowered = model_policy.GENERATION_MODE
                text = lowered
            if (group, key) in (
                ("llm", "binding"),
                ("llm", "model"),
                ("llm", "host"),
                ("embedding", "binding"),
                ("embedding", "model"),
                ("embedding", "host"),
            ):
                expected = {
                    ("llm", "binding"): model_policy.DEEPSEEK_BINDING,
                    ("llm", "model"): model_policy.DEEPSEEK_MODEL,
                    ("llm", "host"): model_policy.DEEPSEEK_BASE_URL,
                    ("embedding", "binding"): model_policy.EMBEDDING_BINDING,
                    ("embedding", "model"): model_policy.EMBEDDING_MODEL,
                    ("embedding", "host"): model_policy.EMBEDDING_BASE_URL,
                }[(group, key)]
                if text and text != expected:
                    errors.append(
                        f"{ENV_LABELS.get((group, key), key)} 已锁定为 {expected}"
                    )
                text = expected
            if (group, key) == ("embedding", "api_key"):
                text = ""
            if (group, key) in NUMERIC_KEYS and text:
                if not text.replace(".", "", 1).isdigit():
                    errors.append(f"{ENV_LABELS.get((group, key), key)} 必须是数字")
                    text = ""
            if (group, key) == ("ingest", "confirm") and text:
                # Phase 15：入库确认门
                text = text.lower()
                if text not in INGEST_CONFIRM_VALUES:
                    errors.append("入库确认方式只能是 always / auto / off")
                    text = ""
            if (group, key) == ("inbox", "confirm") and text:
                # Phase 16：文件夹轮询的确认门（同一个取值域，只是作用范围不同）
                text = text.lower()
                if text not in INBOX_CONFIRM_VALUES:
                    errors.append("文件夹入库确认方式只能是 always / auto / off")
                    text = ""
            if (group, key) == ("quality", "profile") and text:
                text = text.lower()
                if text not in QUALITY_PROFILE_VALUES:
                    errors.append("质量门预设只能是 standard / strict / loose / custom")
                    text = ""
            if (group, key) == ("ocr", "mode") and text:
                text = text.lower()
                if text not in OCR_MODE_VALUES:
                    errors.append("OCR 模式只能是 off / manual / auto")
                    text = ""
            if (group, key) == ("ocr", "provider") and text:
                text = text.lower()
                if text not in OCR_PROVIDER_VALUES:
                    errors.append("OCR 提供方只能是 paddle / local / stub / none")
                    text = ""
            if (group, key) == ("ocr", "device") and text:
                text = text.lower()
                if text not in OCR_DEVICE_VALUES:
                    errors.append("本机 OCR 设备只能是 cpu / gpu")
                    text = ""
            if (group, key) in (("quality", "enabled"), ("ocr", "image_ocr"), ("inbox", "enabled")) and text:
                text = text.lower()
                if text not in TRUTHY_VALUES:
                    errors.append(f"{ENV_LABELS.get((group, key), key)} 只能是 是 / 否（true / false）")
                    text = ""
            merged_section[key] = text
        merged[group] = merged_section
    return _finalize(merged), errors


def save(data: Dict[str, Any]) -> Dict[str, Any]:
    # 写入前先备份，便于一键回退
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.is_file():
        try:
            shutil.copyfile(SETTINGS_FILE, BACKUP_FILE)
        except OSError:
            pass
    payload = json.loads(json.dumps(data, ensure_ascii=False))
    payload["version"] = VERSION
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    SETTINGS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8"
    )
    return payload


def lightrag_home(data: Dict[str, Any]) -> Path:
    # LightRAG 安装目录：系统环境变量 > 用户配置 > 默认
    from os import environ
    value = environ.get(LIGHTRAG_HOME_ENV) or _normalize_scalar(
        data.get("paths", {}).get("lightrag_home")
    )
    if not value:
        value = str(BASE_DIR / "lightrag") if (BASE_DIR / "lightrag").is_dir() else "./lightrag"
    return Path(value)


def effective_lightrag_env(data: Dict[str, Any]) -> Dict[str, str]:
    # 读取 LightRAG 自身 .env，仅用于页面展示“当前实际生效值”
    from app.core.config import parse_env_file
    return parse_env_file(lightrag_home(data) / ".env")


def to_env(data: Dict[str, Any], target: str = "lightrag") -> Dict[str, str]:
    # 生成要注入子进程的环境变量；空值不注入（保持上游原有配置）
    env: Dict[str, str] = {}
    mapping = APP_ENV_MAP if target == "app" else KEY_ENV_MAP
    for (group, key), env_name in mapping.items():
        value = data.get(group, {}).get(key)
        if isinstance(value, bool):
            env[env_name] = "true" if value else "false"
            continue
        text = _normalize_scalar(value)
        if text:
            env[env_name] = text
    if target == "lightrag":
        env["LLM_BINDING"] = model_policy.DEEPSEEK_BINDING
        env["LLM_MODEL"] = model_policy.DEEPSEEK_MODEL
        env["LLM_BINDING_HOST"] = model_policy.DEEPSEEK_BASE_URL
        env["EMBEDDING_BINDING"] = model_policy.EMBEDDING_BINDING
        env["EMBEDDING_MODEL"] = model_policy.EMBEDDING_MODEL
        env["EMBEDDING_BINDING_HOST"] = model_policy.EMBEDDING_BASE_URL
        env.pop("EMBEDDING_BINDING_API_KEY", None)
    else:
        env["GENERATION_MODE"] = model_policy.GENERATION_MODE
    if target != "app":
        store_type = _normalize_scalar(data.get("vector_store", {}).get("type")).lower()
        if store_type in VECTOR_STORAGE_VALUES:
            env[VECTOR_STORAGE_ENV] = VECTOR_STORAGE_VALUES[store_type]
        qdrant_url = _normalize_scalar(data.get("vector_store", {}).get("qdrant_url"))
        if qdrant_url:
            env[QDRANT_URL_ENV] = qdrant_url
    return env


def _is_secret_name(name: str) -> bool:
    # 展示用副本：API Key / Token / Secret 一律只留尾部 4 位
    upper = name.upper()
    return upper.endswith(("API_KEY", "TOKEN", "_KEY", "SECRET"))


def mask_env(env: Dict[str, str]) -> Dict[str, str]:
    # 展示用副本：密钥掩码
    result: Dict[str, str] = {}
    for name, value in env.items():
        if _is_secret_name(name) and value:
            result[name] = ("******" + value[-4:]) if len(value) >= 8 else "已配置"
        else:
            result[name] = value
    return result
