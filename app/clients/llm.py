"""P3A/P3C：本地 LLM 客户端（Ollama，支持非流式与流式）。

边界：
- **只允许回环地址**（`localhost` / 127.0.0.0/8 / `::1`）：不把问题或证据发到任何外部服务；
- `LOCAL_LLM_ENABLED` 默认 off：未显式开启时 `chat()` / `stream_chat()` / `probe()`
  直接拒绝，**不发请求**；
- 不做云端鉴权（不读 API Key），不重试生成请求（重试会重复消耗模型时间）；
- 错误信息不包含提示词、证据正文或回答正文。

P3A 时本模块只被链路脚本与测试导入；P3B 起，`GENERATION_MODE=local`
时生产门面（`app/services/local_generation.py`）会构造受控的启用副本调用它。
默认 `upstream` 模式仍然不会导入或调用本模块。

P3C：`stream_chat()` 用 Ollama `stream=true` 逐 token 产出，支持外部取消；
首 token 与总耗时由调用方（`local_chain`）记录。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from app.core.config import Settings, get_settings

CHAT_PATH = "/api/chat"
TAGS_PATH = "/api/tags"

#: P3A 固定生成参数：温度 0 + 固定种子，尽量降低同一输入的答案漂移。
DEFAULT_TEMPERATURE = 0.0
DEFAULT_SEED = 42
ALLOWED_ROLES = ("system", "user", "assistant")


class LLMError(Exception):
    """本地模型不可用 / 配置不合法；`kind` 供脚本区分错误类别。"""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "llm_error",
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code


@dataclass(frozen=True)
class LLMReply:
    content: str
    model: str
    elapsed_ms: float
    usage: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMStreamChunk:
    """流式增量；`done=True` 的最后一帧携带 usage，content 可能为空。"""

    content: str
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    done: bool = False


def _settings_or_default(settings: Optional[Settings]) -> Settings:
    return settings if settings is not None else get_settings()


def _check_enabled(settings: Settings) -> None:
    if not settings.llm_enabled:
        raise LLMError(
            "本地生成未启用（LOCAL_LLM_ENABLED=off 或未设置）",
            kind="llm_disabled",
        )


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    """校验并归一化 Ollama 地址；P3A 只接受无路径、无鉴权的回环地址。"""
    text = str(value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise LLMError(
            "LOCAL_LLM_BASE_URL 必须是 http(s) 地址",
            kind="llm_bad_base_url",
        )
    if parsed.username or parsed.password:
        raise LLMError(
            "LOCAL_LLM_BASE_URL 不能包含账号密码（P3A 只支持本机无鉴权 Ollama）",
            kind="llm_bad_base_url",
        )
    host = (parsed.hostname or "").lower()
    if not _is_loopback(host):
        raise LLMError(
            "P3A 只允许连接本机 Ollama（localhost / 127.0.0.1 / ::1）；"
            f"当前地址 {host or text} 不是回环地址",
            kind="llm_not_local",
        )
    if parsed.path.strip("/"):
        raise LLMError(
            "LOCAL_LLM_BASE_URL 只能是源地址，不能带路径",
            kind="llm_bad_base_url",
        )
    return text


def _timeout(settings: Settings) -> Tuple[int, int]:
    return (
        max(1, settings.llm_connect_timeout),
        max(1, settings.llm_timeout),
    )


def _probe_timeout(settings: Settings) -> Tuple[int, int]:
    return (max(1, settings.llm_connect_timeout), min(10, max(1, settings.llm_timeout)))


def _http_error(settings: Settings, model: str, status: int) -> LLMError:
    # 刻意不带上游响应正文：防止把提示词/回答经由错误信息带出去。
    return LLMError(
        f"本地模型返回错误（HTTP {status}）：{model}（{settings.llm_base_url}）",
        kind="llm_http_error",
        status_code=status,
    )


def _request_error(
    settings: Settings,
    exc: requests.exceptions.RequestException,
    model: Optional[str] = None,
) -> LLMError:
    name = model or settings.llm_model
    if isinstance(exc, requests.exceptions.Timeout):
        return LLMError(
            f"本地模型响应超时（{settings.llm_timeout}s）：{name}",
            kind="llm_timeout",
        )
    return LLMError(
        f"连不上本机 Ollama（{settings.llm_base_url}），请确认 11434 已启动",
        kind="llm_unavailable",
    )


def _read_json(response: Any) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMError("本地模型返回内容不是 JSON", kind="llm_bad_response") from exc
    if not isinstance(payload, dict):
        raise LLMError("本地模型返回的 JSON 顶层不是对象", kind="llm_bad_response")
    return payload


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ms(value: Any) -> float:
    """Ollama 的 *_duration 字段单位是纳秒。"""
    try:
        return round(float(value) / 1_000_000, 1)
    except (TypeError, ValueError):
        return 0.0


def _model_present(models: Sequence[str], model: str) -> bool:
    return any(name == model or name == f"{model}:latest" for name in models)


def _usage_from(data: Mapping[str, Any]) -> Dict[str, Any]:
    """把 Ollama 的计数/纳秒耗时归一成稳定字段。

    `done_reason=length` 表示回答被 `num_predict` 截断，P3C.2 内容校验据此
    判定“不完整”；其它值（通常是 `stop`）原样保留，便于调优统计。
    """
    return {
        "prompt_eval_count": _as_int(data.get("prompt_eval_count")),
        "eval_count": _as_int(data.get("eval_count")),
        "total_duration_ms": _ms(data.get("total_duration")),
        "load_duration_ms": _ms(data.get("load_duration")),
        "prompt_eval_duration_ms": _ms(data.get("prompt_eval_duration")),
        "eval_duration_ms": _ms(data.get("eval_duration")),
        "done_reason": str(data.get("done_reason") or ""),
    }


def _temperature(settings: Settings) -> float:
    """温度默认 0；配置非法时回落到默认值，避免把坏值发给 Ollama。"""
    value = getattr(settings, "llm_temperature", DEFAULT_TEMPERATURE)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TEMPERATURE
    return number if 0.0 <= number <= 2.0 else DEFAULT_TEMPERATURE


def _options(settings: Settings, num_ctx: int) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "temperature": _temperature(settings),
        "seed": DEFAULT_SEED,
        "num_ctx": num_ctx,
        "num_predict": settings.llm_num_predict,
    }
    return options


def _keep_alive(settings: Settings) -> str:
    return str(getattr(settings, "llm_keep_alive", "") or "").strip()


def _validate_messages(messages: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise LLMError("messages 不能为空", kind="llm_bad_input")
    clean: List[Dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise LLMError("messages 里的每一条都必须是对象", kind="llm_bad_input")
        role = item.get("role")
        content = item.get("content")
        if role not in ALLOWED_ROLES:
            raise LLMError(f"不支持的消息角色：{role}", kind="llm_bad_input")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("messages 的 content 不能为空", kind="llm_bad_input")
        clean.append({"role": str(role), "content": content})
    if not any(item["role"] == "user" for item in clean):
        raise LLMError("messages 至少需要一条 user 消息", kind="llm_bad_input")
    return clean


def probe(
    *,
    settings: Optional[Settings] = None,
    session: Any = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """检查本机 Ollama 与目标模型是否存在；不生成、不改状态。"""
    cfg = _settings_or_default(settings)
    _check_enabled(cfg)
    base = normalize_base_url(cfg.llm_base_url)
    url = base + TAGS_PATH
    name = str(model if model is not None else cfg.llm_model).strip()
    if not name:
        raise LLMError("LOCAL_LLM_MODEL 不能为空", kind="llm_bad_model")

    try:
        response = (session or requests).get(url, timeout=_probe_timeout(cfg))
    except requests.exceptions.RequestException as exc:
        raise _request_error(cfg, exc, name) from exc

    if response.status_code >= 400:
        raise _http_error(cfg, name, response.status_code)

    payload = _read_json(response)
    raw_models = payload.get("models")
    models = [
        str(item.get("name") or item.get("model") or "")
        for item in (raw_models if isinstance(raw_models, list) else [])
        if isinstance(item, dict)
    ]
    models = [item for item in models if item]
    return {
        "base_url": base,
        "models": models,
        "model": name,
        "model_present": _model_present(models, name),
    }


def chat(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
    session: Any = None,
    num_ctx: Optional[int] = None,
) -> LLMReply:
    """调用 Ollama `POST /api/chat`（非流式），返回回答与用量。"""
    cfg = _settings_or_default(settings)
    _check_enabled(cfg)
    base = normalize_base_url(cfg.llm_base_url)

    clean = _validate_messages(messages)
    name = str(model if model is not None else cfg.llm_model).strip()
    if not name:
        raise LLMError("LOCAL_LLM_MODEL 不能为空", kind="llm_bad_model")

    ctx = cfg.llm_num_ctx if num_ctx is None else num_ctx
    if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx < 512:
        raise LLMError("num_ctx 必须是 >= 512 的整数", kind="llm_bad_input")

    payload = {
        "model": name,
        "messages": clean,
        "stream": False,
        "options": _options(cfg, ctx),
    }
    keep_alive = _keep_alive(cfg)
    if keep_alive:
        payload["keep_alive"] = keep_alive

    started = time.perf_counter()
    try:
        response = (session or requests).post(
            base + CHAT_PATH, json=payload, timeout=_timeout(cfg)
        )
    except requests.exceptions.RequestException as exc:
        raise _request_error(cfg, exc, name) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    if response.status_code >= 400:
        raise _http_error(cfg, name, response.status_code)

    data = _read_json(response)
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else ""
    if not isinstance(content, str) or not content.strip():
        raise LLMError("本地模型没有返回回答", kind="llm_bad_response")

    usage = _usage_from(data)
    return LLMReply(
        content=content.strip(),
        model=str(data.get("model") or name),
        elapsed_ms=elapsed_ms,
        usage=usage,
    )


def stream_chat(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: Optional[str] = None,
    settings: Optional[Settings] = None,
    session: Any = None,
    num_ctx: Optional[int] = None,
    cancel: Optional[Callable[[], bool]] = None,
    first_token_timeout: Optional[float] = None,
) -> Iterator[LLMStreamChunk]:
    """调用 Ollama `POST /api/chat`（stream=true），逐帧产出增量。

    取消/超时/HTTP 错误都抛 `LLMError`；错误信息不含响应正文。
    最后一帧 `done=True` 携带 usage；若模型只返回空内容则视为坏响应。
    `first_token_timeout`（P3C.3）：从请求开始到第一个内容 token 的上限，
    超时由看门狗关闭响应并抛 `llm_first_token_timeout`，供调用方回退上游。
    """
    cfg = _settings_or_default(settings)
    _check_enabled(cfg)
    base = normalize_base_url(cfg.llm_base_url)

    if cancel is not None and cancel():
        raise LLMError("本机生成已取消", kind="llm_cancelled")

    clean = _validate_messages(messages)
    name = str(model if model is not None else cfg.llm_model).strip()
    if not name:
        raise LLMError("LOCAL_LLM_MODEL 不能为空", kind="llm_bad_model")

    ctx = cfg.llm_num_ctx if num_ctx is None else num_ctx
    if not isinstance(ctx, int) or isinstance(ctx, bool) or ctx < 512:
        raise LLMError("num_ctx 必须是 >= 512 的整数", kind="llm_bad_input")

    payload = {
        "model": name,
        "messages": clean,
        "stream": True,
        "options": _options(cfg, ctx),
    }
    keep_alive = _keep_alive(cfg)
    if keep_alive:
        payload["keep_alive"] = keep_alive

    first_limit = (
        float(first_token_timeout)
        if isinstance(first_token_timeout, (int, float))
        and not isinstance(first_token_timeout, bool)
        and first_token_timeout > 0
        else 0.0
    )
    started = time.perf_counter()
    # P3C.3：Ollama 可能压着响应头直到首个 token 才发；读超时同时兜住
    # “等响应头”和“等首个 token”，否则看门狗没有 response 可用。
    read_timeout = max(1, int(getattr(cfg, "llm_timeout", 300) or 300))
    if first_limit:
        read_timeout = min(read_timeout, max(1, int(round(first_limit))))
    try:
        response = (session or requests).post(
            base + CHAT_PATH,
            json=payload,
            timeout=(max(1, cfg.llm_connect_timeout), read_timeout),
            stream=True,
        )
    except requests.exceptions.RequestException as exc:
        if first_limit and time.perf_counter() - started >= first_limit * 0.9:
            raise LLMError(
                "本地模型首 token 超时", kind="llm_first_token_timeout"
            ) from exc
        raise _request_error(cfg, exc, name) from exc

    stop_watch = threading.Event()
    first_seen = threading.Event()
    first_timeout = threading.Event()

    def _close() -> None:
        # 先 shutdown 底层 socket：仅调 response.close() 有时打断不了另一个线程里
        # 阻塞中的 iter_lines，会让首 token 超时形同虚设。
        try:
            raw = getattr(response, "raw", None)
            connection = getattr(raw, "_connection", None)
            sock = getattr(connection, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 - 内部结构变化时退回 response.close()
            pass
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 关闭失败时交给读超时
                pass

    def watch_cancel() -> None:
        """取消/首 token 超时时关闭响应，打断阻塞中的 iter_lines；最多延迟 0.2s。"""
        while not stop_watch.wait(0.2):
            try:
                if cancel is not None and cancel():
                    _close()
                    return
            except Exception:  # noqa: BLE001 - 回调异常按“不取消”处理
                return
            if (
                first_limit
                and not first_seen.is_set()
                and time.perf_counter() - started > first_limit
            ):
                first_timeout.set()
                _close()
                return

    if cancel is not None or first_limit:
        threading.Thread(
            target=watch_cancel, name="llm-cancel", daemon=True
        ).start()

    try:
        if response.status_code >= 400:
            raise _http_error(cfg, name, response.status_code)

        deadline = started + max(1, cfg.llm_timeout)
        last_model = name
        usage: Dict[str, Any] = {}
        got_content = False

        try:
            for raw in response.iter_lines():
                if cancel is not None and cancel():
                    raise LLMError("本机生成已取消", kind="llm_cancelled")
                if first_timeout.is_set():
                    raise LLMError(
                        "本地模型首 token 超时", kind="llm_first_token_timeout"
                    )
                if time.perf_counter() > deadline:
                    raise LLMError(
                        f"本地模型流式响应超过 {cfg.llm_timeout}s",
                        kind="llm_timeout",
                    )
                if not raw:
                    continue
                line = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                ).strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError as exc:
                    raise LLMError(
                        "本地模型流式返回内容不是 JSON", kind="llm_bad_response"
                    ) from exc
                if not isinstance(data, dict):
                    raise LLMError(
                        "本地模型流式返回的 JSON 顶层不是对象", kind="llm_bad_response"
                    )
                if data.get("error"):
                    # 刻意不带 error 正文：可能夹带提示词或生成内容。
                    raise LLMError("本地模型流式返回错误", kind="llm_stream_error")

                last_model = str(data.get("model") or last_model)
                message = data.get("message")
                content = message.get("content") if isinstance(message, dict) else ""
                if isinstance(content, str) and content:
                    got_content = True
                    first_seen.set()

                if data.get("done"):
                    if not got_content:
                        raise LLMError("本地模型没有返回回答", kind="llm_bad_response")
                    usage = _usage_from(data)
                    yield LLMStreamChunk(
                        content=content if isinstance(content, str) else "",
                        model=last_model,
                        usage=usage,
                        done=True,
                    )
                    return
                if isinstance(content, str) and content:
                    yield LLMStreamChunk(content=content, model=last_model)
        except Exception as exc:
            # 看门狗关闭响应后，iter_lines 可能抛任意读错误；统一收敛为取消/首 token 超时。
            if first_timeout.is_set():
                raise LLMError(
                    "本地模型首 token 超时", kind="llm_first_token_timeout"
                ) from exc
            if cancel is not None and cancel():
                raise LLMError("本机生成已取消", kind="llm_cancelled") from exc
            if (
                first_limit
                and not got_content
                and time.perf_counter() - started >= first_limit * 0.9
            ):
                raise LLMError(
                    "本地模型首 token 超时", kind="llm_first_token_timeout"
                ) from exc
            if isinstance(exc, requests.exceptions.RequestException):
                raise _request_error(cfg, exc, name) from exc
            raise

        if first_timeout.is_set():
            raise LLMError("本地模型首 token 超时", kind="llm_first_token_timeout")
        if cancel is not None and cancel():
            raise LLMError("本机生成已取消", kind="llm_cancelled")
        if not got_content:
            raise LLMError("本地模型没有返回回答", kind="llm_bad_response")
        # 少数 Ollama 版本不会发 done 帧；按已收到的内容正常收尾。
        yield LLMStreamChunk(content="", model=last_model, usage=usage, done=True)
    finally:
        stop_watch.set()
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主错误
                pass
