"""LightRAG 上游调用出口（Phase 3：超时 + 重试 + 统一异常包装）。

契约（不可变更，否则会破坏与 LightRAG 1.5.6 的对接）：
1. 查询：POST {base}/query            body {"query": ..., "mode": "hybrid"}
2. 上传：POST {base}/documents/upload  multipart 表单字段名 "file"（单数）
3. 探活：GET  {base}/health

设计：
- 复用 requests.Session 并挂载 urllib3 Retry（只对连接错误与 429/5xx 重试，读超时不重试）；
- 连接超时与读超时分别可配（LIGHTRAG_CONNECT_TIMEOUT / LIGHTRAG_TIMEOUT）；
- 网络异常与上游 5xx 统一抛 UpstreamError，由全局异常处理器转成 JSON；
- 正常路径的返回值与 Phase 2 完全一致（/query 直透上游 JSON）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

from app.core.config import BROWSER_UNREACHABLE_HOSTS, get_settings
from app.core.errors import UPSTREAM_TIMEOUT, UpstreamError

QUERY_PATH = "/query"
QUERY_STREAM_PATH = "/query/stream"
# P0：只检索、不生成（LightRAG 1.5.6 的 /query/data）。现有 /query 与 /query/stream 不动。
QUERY_DATA_PATH = "/query/data"
UPLOAD_PATH = "/documents/upload"
HEALTH_PATH = "/health"
TRACK_STATUS_PATH = "/documents/track_status/{track_id}"
STATUS_PATH = "/documents"
# ---------- Phase 16：文档管理（可视化文档管理页 + 删除）----------
# 字段与调用方式全部按 LightRAG 1.5.6 的 OpenAPI 实测核对：
#   POST   /documents/paginated        分页列出文档（**POST**，body 走 DocumentsRequest）
#   GET    /documents/status_counts    各状态计数（processed / failed / … / all）
#   GET    /documents/pipeline_status  管线忙不忙（busy / docs / latest_message …）
#   DELETE /documents/delete_document  按 doc_id 删除（body 是 JSON，不是查询串）
DOCUMENTS_PAGINATED_PATH = "/documents/paginated"
DOCUMENT_STATUS_COUNTS_PATH = "/documents/status_counts"
PIPELINE_STATUS_PATH = "/documents/pipeline_status"
DELETE_DOCUMENT_PATH = "/documents/delete_document"

# LightRAG 1.5.6 的 DocStatus 枚举（少一个都会被前端显示成「未知状态」）
DOCUMENT_STATUSES = (
    "pending",
    "parsing",
    "analyzing",
    "processing",
    "preprocessed",
    "processed",
    "failed",
)

# /documents/paginated 的 page_size 下限：上游 Pydantic 校验为 ge=10，
# 传更小的值会**直接被 422 拒绝**（实测），所以这里夹一下再发。
DOCUMENT_MIN_PAGE_SIZE = 10
DOCUMENT_MAX_PAGE_SIZE = 200

# LightRAG 1.5.6 上传接口的表单字段名（单数）—— 不可更改
UPLOAD_FIELD = "file"

RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_session: Optional[requests.Session] = None
_session_signature: Optional[Tuple[int, int, int]] = None


def base_url() -> str:
    """LightRAG 服务地址（取自 .env 的 LIGHTRAG_URL）。

    注意：这是**服务端进程**用的地址（后端自己发请求用它）。
    要给浏览器用的地址请用 public_base_url() —— 两者在 Docker 与手机场景下并不相同。
    """
    return get_settings().lightrag_url


def _hostname_of(host_header: str) -> str:
    """从 Host 头里取出主机名（去掉端口；IPv6 的方括号一并去掉）。"""
    text = (host_header or "").strip()
    if not text:
        return ""
    if text.startswith("["):                     # 例如 [::1]:8100
        end = text.find("]")
        return text[1:end] if end > 0 else ""
    if text.count(":") == 1:                     # 例如 192.168.3.4:8100
        return text.rsplit(":", 1)[0]
    return text


def public_base_url(request_host: str = "") -> str:
    """浏览器真正能打开的 LightRAG 地址（供「知识图谱」使用）。

    为什么不能直接用 base_url()：它是给本机进程/容器用的，浏览器未必打得开 ——
      · `http://127.0.0.1:9621`：手机上 127.0.0.1 指的是手机自己，局域网访问时图谱打不开；
      · `http://host.docker.internal:9621`：这个主机名只有容器内部认，Docker 模式下必然打不开。

    取值优先级：
      1. LIGHTRAG_PUBLIC_URL（显式配置，最可靠；反代 / 特殊网络请填它）；
      2. LIGHTRAG_URL 的主机名属于 BROWSER_UNREACHABLE_HOSTS 时，改用**浏览器访问本应用时用的
         主机名**，端口与协议沿用 LIGHTRAG_URL —— 本机、局域网（手机）、Docker 三种场景都成立；
      3. 其余情况沿用 LIGHTRAG_URL（LightRAG 本来就在别的机器上，浏览器大概率能直接访问）。
    """
    settings = get_settings()

    configured = (settings.lightrag_public_url or "").strip()
    if configured:
        return configured.rstrip("/")

    base = base_url().rstrip("/")
    parts = urlsplit(base)
    host = (parts.hostname or "").strip().lower()
    if not host or host not in BROWSER_UNREACHABLE_HOSTS:
        return base

    browser_host = _hostname_of(request_host)
    if not browser_host:
        return base

    netloc = browser_host if parts.port is None else f"{browser_host}:{parts.port}"
    return f"{parts.scheme or 'http'}://{netloc}"


def webui_url(request_host: str = "") -> str:
    """LightRAG 自带 WebUI 的地址（「知识图谱」就是它 knowledge-graph 标签里的视图）。

    这个 WebUI 是一个 SPA（HashRouter 只有 /login 与 /*），知识图谱**没有独立地址**，
    所以「在别人页面里显示图谱」只能嵌 /webui/ 本身（它不送 X-Frame-Options / CSP，
    允许被 iframe；且 iframe 的文档源仍是 LightRAG，它自己的接口调用不受跨域影响）。
    """
    return public_base_url(request_host) + "/webui/"


GRAPH_LABELS_PATH = "/graph/label/list"
GRAPH_LABEL_SEARCH_PATH = "/graph/label/search"
GRAPH_LABEL_POPULAR_PATH = "/graph/label/popular"
GRAPHS_PATH = "/graphs"


def _get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET 上游并返回 JSON（只读接口统一走这里，超时/错误包装与其他调用一致）。

    注意返回类型可能是 list（`/graph/label/*`）也可能是 dict（`/graphs`），
    所以这里不套 `_read_json` 的 Dict 假设。
    """
    url = base_url() + path
    try:
        response = get_session().get(url, params=params, timeout=_timeout())
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc
    if response.status_code >= 400:
        status = response.status_code
        body = response.text[:500]
        response.close()
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {status}）",
            upstream_status=status,
            upstream_body=body,
        )
    try:
        return response.json()
    finally:
        response.close()


# ---------- 知识图谱（只读；自建图谱页用）----------
# 我们不再把 LightRAG 自带 WebUI 嵌给用户看，原因是那个界面里能上传（绕过质量门）、
# 删文档、清空知识库、重建索引，甚至删图谱实体 —— 个人用户误操后无法自行恢复。
# 这里只取"看图"需要的三样东西，全部只读。


def graph_popular(limit: int = 30) -> Any:
    """按连接度取最热门的实体名（`/graph/label/popular`）—— 图谱页的默认入口。"""
    return _get_json(GRAPH_LABEL_POPULAR_PATH, {"limit": limit})


def graph_search(query: str, limit: int = 20) -> Any:
    """模糊搜索实体名（`/graph/label/search`）——「search node in page」。"""
    return _get_json(GRAPH_LABEL_SEARCH_PATH, {"q": query, "limit": limit})


def graph_global(max_nodes: int = 400) -> Any:
    """取**整张**知识图谱（`/graphs` + `label="*"`）。

    `node_label == "*"` 是 LightRAG 自己的"全图"约定，7 个存储后端（networkx / neo4j /
    postgres / mongo / milvus / memgraph / opensearch）都实现了同一条分支；实测
    `/graphs?label=*&max_depth=3&max_nodes=400` 返回整图且忽略 depth（`is_truncated` 会
    告诉我们有没有被 max_nodes 截断）。上游还会用它自己的 max_graph_nodes 再兜一层。
    """
    return _get_json(GRAPHS_PATH, {"label": "*", "max_depth": 3, "max_nodes": max_nodes})


def graph_subgraph(label: str, depth: int = 2, max_nodes: int = 60) -> Any:
    """以某实体为中心的连通子图（`/graphs`）→ `{nodes, edges, is_truncated}`。

    节点的 `properties` 里直接带 `file_path`（来源文档）与 `source_id`（来源片段），
    所以「从节点跳到相关文档」不需要额外接口。
    """
    return _get_json(
        GRAPHS_PATH,
        {"label": label, "max_depth": depth, "max_nodes": max_nodes},
    )


def _timeout() -> Tuple[int, int]:
    settings = get_settings()
    return (settings.lightrag_connect_timeout, settings.lightrag_timeout)


def _build_session(settings) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=settings.lightrag_retries,
        connect=settings.lightrag_retries,
        read=0,                      # 读超时不重试：避免长查询被重复发起
        status=settings.lightrag_retries,
        backoff_factor=0.5,
        status_forcelist=sorted(RETRY_STATUS_CODES),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session() -> requests.Session:
    """获取并缓存 HTTP 会话；配置变更后自动重建。"""
    global _session, _session_signature

    settings = get_settings()
    signature = (
        settings.lightrag_retries,
        settings.lightrag_timeout,
        settings.lightrag_connect_timeout,
    )

    if _session is None or _session_signature != signature:
        _session = _build_session(settings)
        _session_signature = signature

    return _session


def _to_upstream_error(exc: requests.exceptions.RequestException, url: str) -> UpstreamError:
    """把 requests 异常翻译为统一的 UpstreamError。"""
    if isinstance(exc, requests.exceptions.Timeout):
        return UpstreamError(
            f"知识库查询超时（{url}），请稍后重试",
            kind=UPSTREAM_TIMEOUT,
            status_code=504,
        )
    return UpstreamError(
        f"知识库服务不可用（{url}），请确认 LightRAG(9621) 已启动"
    )


def _read_json(response: requests.Response, url: str) -> Dict[str, Any]:
    """解析上游 JSON；失败时抛 UpstreamError（附状态码与响应片段）。"""
    try:
        return response.json()
    except ValueError as exc:
        raise UpstreamError(
            f"知识库返回内容无法解析为 JSON（{url}）",
            upstream_status=response.status_code,
            upstream_body=response.text,
        ) from exc


# 查询模式：锁定为 mix（= LightRAG WebUI 检索面板的默认值）。
# 不提供配置项：两侧不一致会让"同一个知识库出现两套答案"，而个人用户没有理由去调它。
QUERY_MODE = "mix"

# P0：/query/data 允许的检索模式（与 LightRAG 1.5.6 QueryRequest.mode 的枚举一致）。
# 默认仍走 QUERY_MODE；这里单独暴露是为了影子和调试时可以临时换模式。
QUERY_MODES = ("local", "global", "hybrid", "naive", "mix", "bypass")


def _clean_history(
    history: Optional[Sequence[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """收敛传给 LightRAG 的近期对话：user/assistant、非空、最多 40 条。"""
    if not isinstance(history, (list, tuple)):
        return []
    result: List[Dict[str, str]] = []
    for item in history[-40:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        result.append({"role": role, "content": content[:8000]})
    return result


def _query_payload(
    message: str,
    *,
    stream: bool,
    include_references: Optional[bool] = None,
    conversation_history: Optional[Sequence[Dict[str, Any]]] = None,
    user_prompt: Optional[str] = None,
    mode: str = QUERY_MODE,
) -> Dict[str, Any]:
    """构造查询请求体（/query 与 /query/stream 共用，保证两条路径参数完全一致）。

    除了 query / mode，还**显式下发检索参数**。这些是本项目认准的推荐值，与 LightRAG
    WebUI 检索面板的默认值一致（40 / 20 / 6000 / 8000 / 30000）；显式下发的好处是不再
    依赖上游 .env 的历史遗留值，换一台机器行为一致。
    注意：**故意不发 enable_rerank**。上游这个字段的默认值来自它自己的 `.env`
    `RERANK_BY_DEFAULT`（未设置时默认 true），"用哪家 / 是否可用"由 `RERANK_BINDING` 决定
    （设置页 ③ 重排序 → 接入方式选 `off` 即完全不启用、不外发）。
    我们一旦在这里写死 true，就等于替用户永久决定了"允许把片段外发去重排序"。

    P2B：`include_references` 为 `None` 时沿用配置（默认 false，行为与改造前一致）；
    显式 True/False 只影响本次 /query 调用，不改变 /chat 的默认路径。
    """
    if include_references is not None and not isinstance(include_references, bool):
        raise ValueError("include_references 必须是布尔值或 None")
    if mode not in QUERY_MODES:
        raise ValueError(f"mode 必须是 {QUERY_MODES} 之一")

    settings = get_settings()
    payload: Dict[str, Any] = {
        "query": message,
        "mode": mode,
        "top_k": settings.lightrag_top_k,
        "chunk_top_k": settings.lightrag_chunk_top_k,
        "max_entity_tokens": settings.lightrag_max_entity_tokens,
        "max_relation_tokens": settings.lightrag_max_relation_tokens,
        "max_total_tokens": settings.lightrag_max_total_tokens,
    }
    history = _clean_history(conversation_history)
    if history:
        payload["conversation_history"] = history
    prompt = str(user_prompt or "").strip()
    if prompt:
        payload["user_prompt"] = prompt[:24000]

    if stream:
        payload["stream"] = True
        payload["include_references"] = True
    else:
        want = (
            settings.lightrag_include_references
            if include_references is None
            else include_references
        )
        if want:
            # 默认 false → 与改造前完全一致；置 true 才要求上游附带 references
            payload["include_references"] = True
    return payload


def query(
    message: str,
    *,
    include_references: Optional[bool] = None,
    conversation_history: Optional[Sequence[Dict[str, Any]]] = None,
    user_prompt: Optional[str] = None,
    mode: str = QUERY_MODE,
) -> Dict[str, Any]:
    """调用 POST /query，返回上游原始 JSON（正常路径与改造前一致）。

    `include_references` 缺省时沿用配置；P2B 的回答工具显式传 True，
    现有 `/chat` 不传，因此行为不变。
    `conversation_history` / `user_prompt` 仅在 M2 上下文注入时传入。
    """
    url = base_url() + QUERY_PATH
    payload = _query_payload(
        message,
        stream=False,
        include_references=include_references,
        conversation_history=conversation_history,
        user_prompt=user_prompt,
        mode=mode,
    )

    try:
        response = get_session().post(url, json=payload, timeout=_timeout())
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc

    if response.status_code >= 400:
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {response.status_code}）",
            upstream_status=response.status_code,
            upstream_body=response.text,
        )

    return _read_json(response, url)


def retrieve(
    query: str,
    *,
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    mode: str = QUERY_MODE,
) -> Dict[str, Any]:
    """只检索、不生成：调用 POST /query/data，返回上游原始 JSON。

    这是 P0 的“检索与生成解耦”入口，**不替代** `query()` / `query_stream()`：
    - 请求体沿用 `_query_payload(stream=False)`，与现有问答的检索参数完全一致；
    - `top_k` / `chunk_top_k` 只在显式传入时覆盖，默认值仍取配置；
    - `mode` 默认 mix，只接受 `QUERY_MODES` 里的值；
    - 4xx / 5xx / 连不上统一抛 `UpstreamError`（与其它只读接口一致），
      由调用方决定是展示还是降级。

    刻意不做的事：不裁剪返回结构、不把 chunks 拼成答案、不读写本地文件。
    """
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    text = query.strip()
    if len(text) < 3:
        # LightRAG QueryRequest.query 的下限是 3；提前拦住，避免一次无意义的 422。
        raise ValueError("query 至少需要 3 个字符")
    if mode not in QUERY_MODES:
        raise ValueError(f"不支持的检索模式：{mode}")

    payload = _query_payload(text, stream=False)
    if mode != QUERY_MODE:
        payload["mode"] = mode
    if top_k is not None:
        payload["top_k"] = _positive_int(top_k, "top_k")
    if chunk_top_k is not None:
        payload["chunk_top_k"] = _positive_int(chunk_top_k, "chunk_top_k")

    return _json_call("POST", QUERY_DATA_PATH, payload)


def _positive_int(value: Any, name: str) -> int:
    """校验正整数参数；bool 不算（True 会被当成 1）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def upload(filename, content, content_type):
    """调用 POST /documents/upload，返回原始响应对象。

    4xx（例如文件类型不被上游支持）交由路由据此报告 success=false，
    以保持改造前 success/filename/message/lightrag 的返回结构；
    连接失败 / 超时 / 上游 5xx 一律抛 UpstreamError（属服务故障）。
    """
    url = base_url() + UPLOAD_PATH

    try:
        response = get_session().post(
            url,
            files={
                UPLOAD_FIELD: (filename, content, content_type)
            },
            timeout=_timeout(),
        )
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc

    if response.status_code >= 500:
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {response.status_code}）",
            upstream_status=response.status_code,
            upstream_body=response.text,
        )

    return response


def probe_health() -> Dict[str, Any]:
    """探活 LightRAG：GET /health（短超时、不重试，快速失败）。"""
    settings = get_settings()
    url = base_url() + HEALTH_PATH

    result: Dict[str, Any] = {
        "url": url,
        "ok": False,
        "status_code": None,
        "detail": None,
    }

    try:
        response = requests.get(url, timeout=settings.health_timeout)
        result["status_code"] = response.status_code
        result["ok"] = response.status_code < 400
        if not result["ok"]:
            result["detail"] = response.text[:200]
    except requests.exceptions.RequestException as exc:
        result["detail"] = exc.__class__.__name__

    return result


# ============================================================
# M2：LightRAG 是否支持 conversation_history / user_prompt
# ============================================================

_CONTEXT_SUPPORT_LOCK = threading.Lock()
_CONTEXT_SUPPORT: Optional[bool] = None
_CONTEXT_SUPPORT_AT = 0.0
_CONTEXT_SUPPORT_TTL = 60.0


def probe_query_context_support(
    *, force: bool = False, timeout: Optional[float] = None
) -> Optional[bool]:
    """探测上游 OpenAPI 的 QueryRequest 是否包含新字段；失败返回 None。

    结果缓存 60 秒；`None` 也缓存，避免上游故障时每个请求都等一次探测。
    """
    global _CONTEXT_SUPPORT, _CONTEXT_SUPPORT_AT

    with _CONTEXT_SUPPORT_LOCK:
        now = time.monotonic()
        if (
            not force
            and _CONTEXT_SUPPORT_AT
            and now - _CONTEXT_SUPPORT_AT < _CONTEXT_SUPPORT_TTL
        ):
            return _CONTEXT_SUPPORT

    settings = get_settings()
    url = base_url().rstrip("/") + "/openapi.json"
    supported: Optional[bool] = None
    try:
        response = requests.get(
            url,
            timeout=timeout if timeout is not None else min(5, max(1, settings.health_timeout)),
        )
        if response.status_code < 400:
            data = response.json()
            schema = (
                (data.get("components") or {})
                .get("schemas", {})
                .get("QueryRequest", {})
            )
            props = schema.get("properties") if isinstance(schema, dict) else {}
            supported = bool(
                isinstance(props, dict)
                and "conversation_history" in props
                and "user_prompt" in props
            )
    except Exception as exc:  # noqa: BLE001 - 探测失败只降级，不影响聊天
        logger.warning("LightRAG 能力探测失败（忽略）：%s", type(exc).__name__)
        supported = None

    with _CONTEXT_SUPPORT_LOCK:
        _CONTEXT_SUPPORT = supported
        _CONTEXT_SUPPORT_AT = time.monotonic()
    if supported is False:
        logger.warning(
            "LightRAG 不支持 conversation_history/user_prompt："
            "本次降级为不注入记忆/历史（可升级 LightRAG 1.5.6+）"
        )
    return supported


def supports_query_context() -> bool:
    """是否有任何通道允许向上游发送新字段；具体发哪些由 chat 路由分别判断。

    阶段 0 起历史与长期记忆**分别开关**：
      · 两者都 off → 不发送；
      · 任一 on → 能力上允许（具体字段由 _upstream_context_kwargs 过滤）；
      · 都是 auto → 探测 /openapi.json。
    """
    settings = get_settings()
    history = str(getattr(settings, "memory_upstream_history", "off") or "off").lower()
    memory = str(getattr(settings, "memory_upstream_memory", "off") or "off").lower()
    if history == "off" and memory == "off":
        return False
    if history == "on" or memory == "on":
        return True
    return bool(probe_query_context_support())


def reset_query_context_support_cache() -> None:
    """测试/上游恢复后强制重新探测。"""
    global _CONTEXT_SUPPORT, _CONTEXT_SUPPORT_AT
    with _CONTEXT_SUPPORT_LOCK:
        _CONTEXT_SUPPORT = None
        _CONTEXT_SUPPORT_AT = 0.0


def query_stream(
    message: str,
    *,
    conversation_history: Optional[Sequence[Dict[str, Any]]] = None,
    user_prompt: Optional[str] = None,
    mode: str = QUERY_MODE,
) -> Iterator[Dict[str, Any]]:
    # 调用 POST /query/stream，逐行产出上游 NDJSON 解析出的字典
    url = base_url() + QUERY_STREAM_PATH
    payload = _query_payload(
        message,
        stream=True,
        conversation_history=conversation_history,
        user_prompt=user_prompt,
        mode=mode,
    )
    try:
        response = get_session().post(url, json=payload, timeout=_timeout(), stream=True)
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc
    if response.status_code >= 400:
        status = response.status_code
        body = response.text[:500]
        response.close()
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {status}）",
            upstream_status=status,
            upstream_body=body,
        )
    try:
        for raw in response.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue
    finally:
        response.close()




def track_status(
    track_id: str,
    timeout: Optional[Tuple[float, float]] = None,
) -> Dict[str, Any]:
    # 查询某次上传的解析状态（LightRAG: GET /documents/track_status/{track_id}）
    # timeout 可单独传：入库卡片每 2.5 秒轮询一次进度，不能沿用 LIGHTRAG_TIMEOUT（180 秒），
    # 否则知识库一忙，状态接口就跟着卡住，页面看起来像"死机"。
    settings = get_settings()
    url = base_url() + TRACK_STATUS_PATH.format(track_id=track_id)
    try:
        response = get_session().get(
            url,
            timeout=timeout
            or (settings.lightrag_connect_timeout, settings.lightrag_timeout),
        )
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc
    if response.status_code >= 400:
        raise UpstreamError(
            f"查询解析状态失败（HTTP {response.status_code}）",
            upstream_status=response.status_code,
            upstream_body=response.text,
        )
    return _read_json(response, url)


# ============================================================
# Phase 16：文档管理（只读列举 + 删除）
# ============================================================
#
# 为什么要区分「抛异常」与「交回响应对象」：
#   · 列举类接口（分页 / 计数 / 管线状态）是页面要展示的东西，上游坏了就让全局
#     异常处理器回 502/504（页面据此显示「知识库没连上」）—— 走 _call() 抛错；
#   · 删除**不是**：上游会用 4xx 明确告诉我们「为什么不能删」（管线忙 / 找不到该文档），
#     这些是**业务结论**而不是服务故障，必须原样交回路由去说人话
#     （与 upload() 的处理方式一致，见 services/ingest.py 的 humanize_upstream）。

def _call(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> requests.Response:
    """统一的上游调用：超时 / 连不上 / 上游 5xx 一律抛 UpstreamError；4xx 交回调用方。"""
    url = base_url() + path
    try:
        response = get_session().request(
            method, url, json=payload, params=params, timeout=_timeout()
        )
    except requests.exceptions.RequestException as exc:
        raise _to_upstream_error(exc, url) from exc

    if response.status_code >= 500:
        status = response.status_code
        body = response.text[:500]
        response.close()
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {status}）",
            upstream_status=status,
            upstream_body=body,
        )
    return response


def _json_call(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """比 _call 多一步：4xx 也当错误（用于纯读取接口），成功则返回解析后的 JSON。"""
    response = _call(method, path, payload, params)
    if response.status_code >= 400:
        status = response.status_code
        body = response.text[:500]
        response.close()
        raise UpstreamError(
            f"知识库服务返回错误（HTTP {status}）",
            upstream_status=status,
            upstream_body=body,
        )
    try:
        return response.json()
    finally:
        response.close()


def clamp_page_size(page_size: int) -> int:
    """把分页大小夹进上游允许的区间（否则会被 422 拒绝，页面看到「查询失败」）。"""
    try:
        value = int(page_size)
    except (TypeError, ValueError):
        value = DOCUMENT_MIN_PAGE_SIZE
    return max(DOCUMENT_MIN_PAGE_SIZE, min(DOCUMENT_MAX_PAGE_SIZE, value))


def documents_paginated(
    *,
    page: int = 1,
    page_size: int = 50,
    status: str = "",
    sort_field: str = "updated_at",
    sort_direction: str = "desc",
) -> Dict[str, Any]:
    """分页列出知识库里的文档（POST /documents/paginated）。

    返回上游原文：`{documents: [...], pagination: {...}, status_counts: {...}}`。
    `status` 留空 = 不筛状态；只接受 DOCUMENT_STATUSES 里的值（非法值当没填）。
    """
    try:
        current_page = max(1, int(page))
    except (TypeError, ValueError):
        current_page = 1

    wanted = (status or "").strip().lower()
    payload: Dict[str, Any] = {
        "page": current_page,
        "page_size": clamp_page_size(page_size),
        "sort_field": sort_field or "updated_at",
        "sort_direction": "asc" if str(sort_direction).lower() == "asc" else "desc",
    }
    if wanted in DOCUMENT_STATUSES:
        payload["status_filter"] = wanted

    return _json_call("POST", DOCUMENTS_PAGINATED_PATH, payload)


def status_counts() -> Dict[str, Any]:
    """各状态文档数（GET /documents/status_counts）→ `{status_counts: {...}}`。"""
    return _json_call("GET", DOCUMENT_STATUS_COUNTS_PATH)


def pipeline_status() -> Dict[str, Any]:
    """管线状态（GET /documents/pipeline_status）：busy / docs / latest_message / recovery_*。

    删除文档是**异步**的：管线忙时上游会拒绝删除请求，页面据此告诉用户「稍后再试」。
    """
    return _json_call("GET", PIPELINE_STATUS_PATH)


def delete_document(
    doc_ids: List[str],
    *,
    delete_file: bool = False,
    delete_llm_cache: bool = False,
) -> requests.Response:
    """删除文档（DELETE /documents/delete_document，body 是 JSON）。

    · `delete_file=True` 会**连带删掉上游 input 目录里我们上传的那份 .md 拷贝**
      （唯一会碰到 lightrag/ 的边界，页面上必须写清楚，默认关闭）；
    · 返回原始响应对象：4xx（管线忙 409 / 找不到 404 / 正在删除 …）交由路由翻译成
      人话后回给页面，不当成服务故障。
    """
    payload = {
        "doc_ids": [str(item) for item in doc_ids if str(item).strip()],
        "delete_file": bool(delete_file),
        "delete_llm_cache": bool(delete_llm_cache),
    }
    return _call("DELETE", DELETE_DOCUMENT_PATH, payload)


def documents_by_name(name: str, *, max_pages: int = 5) -> List[Dict[str, Any]]:
    """按**文档名**在上游反查文档（file_path 与我们上传时的 `<stem>.md` 同名）。

    用途：本地作业档案里没有 doc_id 时（数据是旧版本写的），删除前需要拿到 doc_id。
    只在删除这种低频路径上调用；最多翻 `max_pages` 页，翻不到就返回空列表。
    """
    wanted = (name or "").strip().lower()
    if not wanted:
        return []
    found: List[Dict[str, Any]] = []
    for page in range(1, max(1, max_pages) + 1):
        payload = documents_paginated(page=page, page_size=100)
        documents = payload.get("documents") or []
        for item in documents:
            if str(item.get("file_path") or "").strip().lower() == wanted:
                found.append(item)
        pagination = payload.get("pagination") or {}
        if not pagination.get("has_next"):
            break
    return found

