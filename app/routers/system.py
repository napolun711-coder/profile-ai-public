"""系统类接口（Phase 2 迁移；Phase 3 新增 GET /health）。

GET /        首页存活探测，返回值原样保留
GET /graph   返回 LightRAG 地址（前端据此在本页嵌「知识图谱」）
             · url   = 浏览器可打开的 LightRAG 基地址
             · webui = 可直接 iframe 的 WebUI 地址（知识图谱是它 knowledge-graph 标签里的视图）
             两者都按请求的 Host 改写，避免 docker 的 host.docker.internal / 手机上的 127.0.0.1
GET /health/live  轻量存活探针：只证明应用已开始接收请求，不探测任何外部依赖
GET /health  新增：聚合探活 LightRAG / Qdrant / Ollama
             本接口始终返回 200（存活探针语义），依赖状态体现在 JSON 里
"""

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.clients import lightrag as lightrag_client
from app.core.config import get_settings
from app.core.probes import probe_tcp
from app.memory import runtime as memory_runtime

router = APIRouter()


@router.get("/")
def home(request: Request):

    """根路径：浏览器打开就进应用，脚本探活仍拿 JSON。

    原来这里只返回 {"status": "AI助手运行中"}，于是用户在地址栏敲 127.0.0.1:8100
    （最自然的输入）看到的是一段裸 JSON，而不是应用 —— 桌面快捷方式走的是 /web，
    但手输地址 / 旧书签 / 从别处复制链接的人都会撞上这一下。

    不用"直接 302 到 /web"是因为这个端点同时承担存活探测（docstring 与行为都写明），
    无条件重定向会让任何跟随跳转的探针拿到 HTML。所以按 Accept 分流：
    浏览器导航一定带 text/html，探针（curl 默认 */*）拿到的还是原来那份 JSON。
    """
    if "text/html" in request.headers.get("accept", ""):

        return RedirectResponse("/web", status_code=302)

    return {
        "status":
        "AI助手运行中"
    }


@router.get("/graph")
def graph(request: Request):

    # 请求头里的 Host 就是「浏览器是怎么访问到本应用的」，用它推导出浏览器可达的 LightRAG 地址
    host = request.headers.get("host", "")

    return {

        "url":

        lightrag_client.public_base_url(host),

        "webui":

        lightrag_client.webui_url(host)

    }


@router.get("/health/live")
def health_live():
    """应用进程已就绪；供一键启动器和容器做快速存活检查。"""
    return {
        "app": "ok",
        "status": "ready"
    }


@router.get("/health")
def health():

    settings = get_settings()

    # 三个探针并发执行：把总耗时收敛到“最慢的单个探针”，避免串行叠加
    with ThreadPoolExecutor(max_workers=3) as executor:

        lightrag_future = executor.submit(
            lightrag_client.probe_health
        )

        qdrant_future = executor.submit(
            probe_tcp,
            settings.qdrant_url,
            settings.health_timeout
        )

        ollama_future = executor.submit(
            probe_tcp,
            settings.ollama_url,
            settings.health_timeout
        )

        lightrag_status = lightrag_future.result()

        qdrant_status = qdrant_future.result()

        ollama_status = ollama_future.result()

    return {

        "status":

        "healthy" if lightrag_status.get("ok") else "degraded",

        "app":

        "ok",

        "lightrag":

        lightrag_status,

        "qdrant":

        qdrant_status,

        "ollama":

        ollama_status,

        # M1：对话持久化状态（关闭时 enabled=false；不暴露任何消息正文）
        "memory":

        memory_runtime.status()

    }
