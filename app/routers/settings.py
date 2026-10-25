"""配置中心接口（Phase 4.2）。

GET  /settings            网页设置页（settings.html）
GET  /api/settings        读取当前用户配置（密钥掩码）+ LightRAG 实际生效值 + 将注入的环境变量
POST /api/settings        校验并保存（保存前自动备份为 config/user-settings.json.bak）
POST /api/settings/test   测试连接（LightRAG / Qdrant / Ollama / 云端模型地址）

注意：本阶段保存后需重启服务才生效（自动重启在 Phase 4.3）。
"""

from __future__ import annotations

from typing import Any, Dict, List

import requests
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from app.clients import lightrag as lightrag_client
from app.core import user_settings
from app.core.config import get_settings
from app.core.paths import INBOX_DIR, INBOX_FAILED_DIR, INBOX_TAKEN_DIR, ORIGINALS_DIR, PROFILE_AI_HOME, STATIC_DIR
from app.core.probes import probe_tcp
from app.services import ocr as ocr_service

router = APIRouter()

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_QDRANT_URL = "http://localhost:6333"


def _effective(data: Dict[str, Any]) -> Dict[str, str]:
    """把用户配置叠加到 LightRAG 自身 .env 上，得到“当前实际生效值”。"""
    effective = dict(user_settings.effective_lightrag_env(data))
    effective.update(user_settings.to_env(data, "lightrag"))
    return effective


def _restart_needed(data: Dict[str, Any]) -> bool:
    """配置文件内容是否与本次进程启动时不同（不同 = 还没重启，改动未生效）。"""
    return user_settings.restart_needed(data)


# Phase 9.2：OCR 相关环境变量 -> 设置页展示名（用于「保存了但还没生效」的提示）
OCR_ENV_LABELS = (
    ("OCR_MODE", "OCR 模式"),
    ("OCR_PROVIDER", "OCR 提供方"),
    ("OCR_IMAGE_OCR", "内嵌图片纳入 OCR"),
    ("OCR_LANG", "识别语言"),
    ("OCR_DEVICE", "本机识别设备"),
    ("OCR_BASE_URL", "云端作业地址"),
    ("OCR_MODEL", "云端模型名"),
    ("OCR_TOKEN", "OCR Token"),
    ("OCR_MAX_FILE_MB", "OCR 单文件上限"),
    ("OCR_LOCAL_MODEL_DIR", "本机模型目录"),
)


def _ocr_preview(data: Dict[str, Any]) -> Dict[str, Any]:
    """按「设置页已保存的值 + 当前 .env」推演重启后的 OCR 状态。

    设置页只能回答「配置写没写进去」，而 OCR 能不能用还取决于提供方与 Token。
    这里把结论直接算给前端，避免出现「点了开启、保存成功、页面却还是显示关」的困惑。
    """
    settings = get_settings()
    injected = user_settings.to_env(data, "app")
    ocr_defaults = user_settings.DEFAULTS["ocr"]

    mode = str(injected.get("OCR_MODE") or settings.ocr_mode or ocr_defaults["mode"] or "off").strip().lower()
    provider = str(injected.get("OCR_PROVIDER") or settings.ocr_provider or "paddle").strip().lower()
    token_set = bool(str(injected.get("OCR_TOKEN") or settings.ocr_token or "").strip())
    known = provider in user_settings.OCR_PROVIDER_VALUES
    enabled = mode in ("manual", "auto") and known and provider != "none" and not (provider == "paddle" and not token_set)

    if mode not in ("manual", "auto"):
        reason = "OCR 模式是 off（要选 manual 或 auto 才会启用）"
    elif provider == "none":
        reason = "提供方设为 none（明确禁用，即使填了 Token 也不外发）"
    elif not known:
        reason = "提供方取值无法识别（应为 paddle / local / stub / none）"
    elif provider == "paddle" and not token_set:
        reason = "云端 PaddleOCR-VL 需要 OCR Token：填好 Token 再保存；不想用云端可把提供方改成「本机 PaddleOCR」"
    else:
        reason = ""

    # 本次进程正在用的值 vs 保存后重启将要用的值（用于列出「还没生效」的项）
    running = {
        "OCR_MODE": settings.ocr_mode,
        "OCR_PROVIDER": settings.ocr_provider,
        "OCR_IMAGE_OCR": "true" if settings.ocr_image_ocr else "false",
        "OCR_LANG": settings.ocr_lang,
        "OCR_DEVICE": settings.ocr_device,
        "OCR_BASE_URL": settings.ocr_base_url,
        "OCR_MODEL": settings.ocr_model,
        "OCR_MAX_FILE_MB": str(settings.ocr_max_file_mb),
        "OCR_LOCAL_MODEL_DIR": settings.ocr_local_model_dir,
    }
    pending = [
        label
        for name, label in OCR_ENV_LABELS
        if name in injected and str(injected[name]).strip() != str(running.get(name, "")).strip()
    ]

    return {
        "configured_mode": mode,
        "configured_provider": provider,
        "token_set": token_set,
        "enabled_after_restart": enabled,
        "external_after_restart": bool(enabled and provider == "paddle"),
        "reason": reason,
        "pending_env": pending,
        "effective_mode": settings.ocr_mode,
        "effective_provider": settings.ocr_provider,
        "effective_enabled": settings.ocr_enabled,
    }


def _payload() -> Dict[str, Any]:
    data = user_settings.load()
    settings = get_settings()
    return {
        "settings": user_settings.mask(data),
        "lightrag_effective": user_settings.mask_env(_effective(data)),
        "lightrag_env_file": str(user_settings.lightrag_home(data) / ".env"),
        "settings_file": str(user_settings.SETTINGS_FILE),
        "settings_exists": user_settings.SETTINGS_FILE.is_file(),
        # Phase 9.2：配置文件在本次进程启动后被改过 = 还没重启，改动未生效
        "restart_needed": _restart_needed(data),
        "injected_env": user_settings.mask_env(user_settings.to_env(data, "lightrag")),
        "injected_app_env": user_settings.mask_env(user_settings.to_env(data, "app")),
        "app_effective": {
            "lightrag_url": settings.lightrag_url,
            "max_upload_size": settings.max_upload_size,
            "allowed_extensions": list(settings.allowed_extensions),
            "log_level": settings.log_level,
            # P2G：进程当前生效值 vs 配置文件保存值（设置页据此提示"保存了但还没重启"）
            "skill_routing": settings.skill_routing,
            "skill_routing_saved": str(
                (data.get("app") or {}).get("skill_routing") or ""
            ),
            "skill_routing_restart_needed": bool(
                str((data.get("app") or {}).get("skill_routing") or "").strip()
                and str((data.get("app") or {}).get("skill_routing") or "").strip().lower()
                != str(settings.skill_routing or "").strip().lower()
            ),
            # P3B：回答生成方式（upstream / local）与本机模型只读信息
            "generation_mode": settings.generation_mode,
            "generation_mode_saved": str(
                (data.get("app") or {}).get("generation_mode") or ""
            ),
            "generation_mode_restart_needed": bool(
                str((data.get("app") or {}).get("generation_mode") or "").strip()
                and str((data.get("app") or {}).get("generation_mode") or "").strip().lower()
                != str(settings.generation_mode or "").strip().lower()
            ),
            "local_llm_model": settings.llm_model,
            "local_llm_base_url": settings.llm_base_url,
            # P3C.3：本机模式保护参数（只读，便于确认重启后的生效值）
            "local_llm_first_token_timeout": settings.llm_first_token_timeout,
            "local_llm_max_concurrency": settings.llm_max_concurrency,
        },
        # Phase 9：质量门与 OCR 的"当前实际生效值"（只读展示，便于对照设置页）
        "quality_effective": {
            "profile": settings.quality_profile,
            # Phase 18：质检恒定开启（QUALITY_ENABLED 已失效）。switch_ignored 是给页面用的：
            # 用户如果在 .env 里关过，要明确告诉他"被忽略了"，而不是默默不生效。
            "enabled": True,
            "switch_ignored": not settings.quality_enabled,
            "min_chars": settings.quality_min_chars,
            "min_readable_ratio": settings.quality_min_readable_ratio,
            "max_garbled_ratio": settings.quality_max_garbled_ratio,
            "max_repeat_ratio": settings.quality_max_repeat_ratio,
            "min_chars_per_page": settings.quality_min_chars_per_page,
            "pass_score": settings.quality_pass_score,
            "max_unocr_images": settings.quality_max_unocr_images,
            "ocr_routes": list(settings.quality_ocr_routes),
        },
        "ocr_effective": {
            "mode": settings.ocr_mode,
            "provider": settings.ocr_provider,
            "enabled": settings.ocr_enabled,
            "external": settings.ocr_external,
            "image_ocr": settings.ocr_image_ocr,
            "lang": settings.ocr_lang,
            "device": settings.ocr_device,
            "base_url": settings.ocr_base_url,
            "model": settings.ocr_model,
            "max_file_mb": settings.ocr_max_file_mb,
            "local_model_dir": settings.ocr_local_model_dir,
            "token_set": bool(settings.ocr_token.strip()),
        },
        "ocr_providers": ocr_service.provider_infos(settings),
        # Phase 15：入库确认门（always / auto / off）——设置页据此显示「当前生效值」
        "ingest_confirm_effective": {
            "mode": settings.ingest_confirm,
            "pending_review_ttl_hours": settings.pending_review_ttl_hours,
            "restart_needed": _restart_needed(data),
        },
        # Phase 16：文件夹轮询（丢进 data\inbox\ 就自动入库）——当前生效值与目录位置
        "inbox_effective": {
            "enabled": settings.inbox_enabled,
            "poll_seconds": settings.inbox_poll_seconds,
            "settle_seconds": settings.inbox_settle_seconds,
            "confirm": settings.inbox_confirm,
            "restart_needed": _restart_needed(data),
            "dirs": {
                "inbox": str(INBOX_DIR),
                "originals": str(ORIGINALS_DIR),
                "taken": str(INBOX_TAKEN_DIR),
                "failed": str(INBOX_FAILED_DIR),
                "home": str(PROFILE_AI_HOME),
                "files_page": "/files",
            },
        },
        # Phase 9.2：重启后的 OCR 结论（能不能用 / 为什么还不能用 / 哪些项还没生效）
        "ocr_preview": _ocr_preview(data),
    }


@router.get("/settings")
def settings_page() -> FileResponse:
    # 网页设置页（与 /api/settings 区分：这里返回 HTML 页面）
    return FileResponse(STATIC_DIR / "settings.html")


@router.get("/api/settings")
def read_settings() -> Dict[str, Any]:
    return _payload()


@router.post("/api/settings")
def write_settings(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    current = user_settings.load()
    # 密钥「清除」单独走一个列表：空字符串仍表示保持原值，不能兼作清除。
    # pop 掉再合并，避免它被当成一个配置分组带进 user-settings.json。
    clear_secrets = payload.pop("clear_secrets", None) if isinstance(payload, dict) else None
    merged, errors = user_settings.validate_and_merge(current, payload)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_settings", "messages": errors},
        )
    if clear_secrets:
        merged = user_settings.apply_secret_clears(merged, clear_secrets)
    saved = user_settings.save(merged)
    result = _payload()
    result["saved"] = True
    result["updated_at"] = saved.get("updated_at")
    result["note"] = "已保存。请重启服务后生效：双击「restart_silent.vbs」（全程无窗口），或先 stop_silent.vbs 再 start_silent.vbs。"
    return result


@router.post("/api/settings/test")
def test_connections() -> Dict[str, Any]:
    data = user_settings.load()
    effective = _effective(data)
    checks: List[Dict[str, Any]] = []

    lightrag_status = lightrag_client.probe_health()
    checks.append({
        "name": "知识库服务 LightRAG",
        "ok": bool(lightrag_status.get("ok")),
        "detail": str(lightrag_status.get("url") or "") + ("（已就绪）" if lightrag_status.get("ok") else "（未就绪，请先启动服务）"),
    })

    store = str(effective.get(user_settings.VECTOR_STORAGE_ENV) or "").lower()
    needs_qdrant = "qdrant" in store
    qdrant_url = str(effective.get(user_settings.QDRANT_URL_ENV) or DEFAULT_QDRANT_URL)
    if needs_qdrant:
        qdrant_status = probe_tcp(qdrant_url, 3)
        checks.append({
            "name": "向量数据库 Qdrant",
            "ok": bool(qdrant_status.get("ok")),
            "detail": qdrant_url if qdrant_status.get("ok") else qdrant_url + "（未连接；若已改为本地文件向量库则无需 Qdrant）",
        })
    else:
        checks.append({"name": "向量数据库 Qdrant", "ok": True, "detail": "当前使用本地文件向量库，无需 Qdrant"})

    binding = str(effective.get("EMBEDDING_BINDING") or "").lower()
    if binding == "ollama":
        ollama_url = str(effective.get("EMBEDDING_BINDING_HOST") or DEFAULT_OLLAMA_URL)
        model = str(effective.get("EMBEDDING_MODEL") or "")
        ok = False
        detail = ollama_url + "（未连接，请启动 Ollama）"
        try:
            response = requests.get(ollama_url.rstrip("/") + "/api/tags", timeout=6)
            names = [str(item.get("name", "")) for item in response.json().get("models", [])]
            hit = any(name == model or name.startswith(model.split(":")[0]) for name in names) if model else bool(names)
            ok = bool(hit)
            detail = ollama_url + "，模型 " + (model or "(未指定)") + (" 已就绪" if hit else " 未找到（请先 ollama pull）")
        except (requests.RequestException, ValueError):
            pass
        checks.append({"name": "本地向量化 Ollama", "ok": ok, "detail": detail})

    llm_binding = str(effective.get("LLM_BINDING") or "").lower()
    llm_host = str(effective.get("LLM_BINDING_HOST") or "")
    llm_model = str(effective.get("LLM_MODEL") or "")
    if llm_binding == "ollama":
        checks.append({"name": "问答大模型", "ok": True, "detail": "使用本地 Ollama 模型 " + (llm_model or "(未指定)")})
    elif llm_host:
        reachable = probe_tcp(llm_host, 5)
        checks.append({
            "name": "问答大模型接口地址",
            "ok": bool(reachable.get("ok")),
            "detail": llm_host + "，模型 " + (llm_model or "(未指定)") + ("（地址可达）" if reachable.get("ok") else "（地址不可达，请检查网络或地址）"),
        })
    else:
        checks.append({"name": "问答大模型接口地址", "ok": False, "detail": "未配置接口地址（LLM_BINDING_HOST）"})

    # P3B：模式为 local 时额外探活本机生成模型（只读；不发送问题或证据）。
    # 用“保存值优先、否则当前生效值”，这样设置页保存后、重启前也能先验证模型。
    injected_app = user_settings.to_env(data, "app")
    generation_mode = str(
        injected_app.get("GENERATION_MODE") or get_settings().generation_mode or "upstream"
    ).strip().lower()
    if generation_mode == "local":
        checks.append(_local_generation_check(get_settings()))

    checks.append(_ocr_check(get_settings()))

    return {"checks": checks, "all_ok": all(item["ok"] for item in checks)}


def _local_generation_check(settings) -> Dict[str, Any]:
    """P3B：本机生成模型探活；失败原因不含问题、提示词、证据或回答正文。"""
    from app.clients.llm import LLMError
    from app.services import local_generation

    try:
        probe = local_generation.probe_model(settings)
    except LLMError as exc:
        return {
            "name": "本机生成模型（Ollama）",
            "ok": False,
            "detail": exc.message,
        }
    except Exception as exc:  # noqa: BLE001 - 探活失败只影响这一项
        return {
            "name": "本机生成模型（Ollama）",
            "ok": False,
            "detail": f"探活异常（{type(exc).__name__}）",
        }

    base_url = str(probe.get("base_url") or "")
    model = str(probe.get("model") or "")
    present = bool(probe.get("model_present"))
    detail = f"{base_url}，模型 {model}" + (
        " 已就绪" if present else " 未找到（请先 ollama pull，或换模型）"
    )
    return {"name": "本机生成模型（Ollama）", "ok": present, "detail": detail}


def _ocr_check(settings) -> Dict[str, Any]:
    """OCR 探活（Phase 9）：只做本地判定与 TCP 连通性，不发送任何文件。"""
    provider = ocr_service.get_provider(settings)

    if not settings.ocr_enabled:
        return {
            "name": "OCR 识别",
            "ok": True,
            "detail": f"未启用（{provider.unavailable_reason(settings)}）——不会向外部发送任何文件",
        }

    if settings.ocr_external:
        reachable = probe_tcp(settings.ocr_base_url, 5)
        detail = f"{provider.label}（会外发文件）：{settings.ocr_model or settings.ocr_base_url}"
        detail += "（地址可达）" if reachable.get("ok") else "（地址不可达，请检查网络或 OCR_BASE_URL）"
        return {"name": "OCR 识别", "ok": bool(reachable.get("ok")), "detail": detail}

    return {
        "name": "OCR 识别",
        "ok": True,
        "detail": f"{provider.label}（不外发）；语言 {settings.ocr_lang}、设备 {settings.ocr_device}",
    }
