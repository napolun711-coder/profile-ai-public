"""应用装配（Phase 2 建立；Phase 3 接入配置化 CORS、访问日志与统一错误处理；
Phase 15 增加启动时的「待确认作业」超时归档；Phase 16 增加文件夹轮询线程）。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.paths import STATIC_DIR, ensure_directories
from app.routers import all_routers
from app.services import inbox as inbox_service
from app.services import ingest as ingest_service

#: 生命周期钩子里的日志（create_app 内部另有局部 logger，那是给请求日志用的）
LOGGER = logging.getLogger(__name__)


class RevalidatingStaticFiles(StaticFiles):
    """静态文件补上 Cache-Control: no-cache（每次都回服务器校验，改完前端立即生效）。

    背景（真实故障）：/static/* 原来没有任何 Cache-Control，浏览器会长期复用旧副本，
    于是出现「磁盘上的 voice.js 已经改了、页面却还在跑旧脚本」的假故障
    （新增的语音设置按钮看不到、点了没反应都属于这一类）。
    这里只加 no-cache（**不是 no-store**）：ETag / Last-Modified 仍然保留，
    内容没变时浏览器拿到 304，代价只是多一次条件请求。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _warn_if_local_unavailable() -> None:
    """P3C.3：本机模式启动自检；只告警，不改模式、不阻塞启动。"""
    try:
        from app.services import local_generation

        if local_generation.generation_mode() != local_generation.MODE_LOCAL:
            return
        probe = local_generation.probe_model()
        if not probe.get("model_present"):
            LOGGER.warning(
                "本机生成已启用，但 Ollama 未提供模型 %s；请求将回退上游",
                str(probe.get("model") or ""),
            )
    except Exception:  # noqa: BLE001 - 自检失败不影响服务启动
        LOGGER.warning(
            "本机生成自检失败：Ollama 不可达或配置异常；请求将回退上游"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭钩子（Phase 15 / Phase 16）。

    启动时做两件事：

    1. 把「等待用户确认入库」超过 `PENDING_REVIEW_TTL_HOURS`（默认 24 小时）的作业
       归档为「已跳过」。确认门是停下来等人的状态，没人答复的作业会一直占着目录、
       下次打开网页看到一堆来路不明的文件；归档只是不入库（原件与解析结果都留着），
       用户随时可以「仍要入库」。
       · 放在线程里跑：要读一批 result.json，别拖住事件循环（失败也只记日志，
         绝不能因为归档出问题就让服务起不来）；
       · 重启即重放：每次启动都扫一遍，所以不存在"只归档一次"的漏网之鱼。

    2. 拉起 `data\\inbox\\` 的轮询线程（Phase 16，`INBOX_ENABLED` 控制）。
       · 只在启动时拉一次（幂等）；关闭服务时停掉它；
       · 起不来（目录不可写等）只记日志，绝不让服务起不来 —— 网页上传仍然可用。
    """
    ensure_directories()
    try:
        archived = await asyncio.to_thread(ingest_service.archive_expired_reviews)
        if archived:
            LOGGER.info("启动归档：%d 个等待确认超时的作业已标记为跳过", len(archived))
    except Exception:  # noqa: BLE001 - 归档失败不影响服务启动
        LOGGER.exception("启动归档待确认作业失败（忽略，不影响服务启动）")

    try:
        if inbox_service.start():
            LOGGER.info("文件夹轮询已启动（收件箱：data\\inbox）")
    except Exception:  # noqa: BLE001 - 轮询起不来不影响其它功能
        LOGGER.exception("文件夹轮询启动失败（忽略；可稍后到「文件管理」页手动扫描）")

    # M1：对话持久化。关闭时不建库；初始化失败只告警，聊天继续（不落盘）。
    try:
        from app.memory import runtime as memory_runtime

        if memory_runtime.initialize():
            store = memory_runtime.get_store()
            repaired = store.repair_stale_streaming() if store is not None else 0
            if repaired:
                LOGGER.info("对话持久化：%d 条中断的流式消息已标记为 partial", repaired)
        elif memory_runtime.enabled():
            LOGGER.warning(
                "对话持久化初始化失败，已降级为不落盘：%s",
                memory_runtime.error() or "unknown",
            )
    except Exception:  # noqa: BLE001 - 记忆库失败不能阻止服务启动
        LOGGER.exception("对话持久化初始化异常（忽略，聊天继续）")

    # M3：记忆任务 worker。三个开关全开才真正启动；失败不影响服务。
    try:
        from app.memory.extract import register_extraction_handler
        from app.memory.worker import get_worker

        worker = get_worker()
        register_extraction_handler(worker)
        worker.start()
    except Exception:  # noqa: BLE001 - worker 起不来不能阻止服务启动
        LOGGER.exception("记忆 worker 启动失败（忽略）")

    # P3C.3：本机模式自检（只告警，不改模式、不阻塞启动）。
    try:
        await asyncio.to_thread(_warn_if_local_unavailable)
    except Exception:  # noqa: BLE001 - 理论不可达；自检绝不能拦住启动
        LOGGER.exception("本机生成自检线程失败（忽略）")

    yield

    try:
        inbox_service.stop()
    except Exception:  # noqa: BLE001 - 退出流程不能被异常打断
        LOGGER.exception("文件夹轮询停止时出错（忽略）")

    try:
        from app.memory.worker import get_worker

        get_worker().stop()
    except Exception:  # noqa: BLE001 - 退出流程不能被异常打断
        LOGGER.exception("记忆 worker 停止时出错（忽略）")


def create_app() -> FastAPI:

    settings = get_settings()

    logger = setup_logging(settings.log_level)

    app = FastAPI(
        title="本地知识库助手",
        lifespan=lifespan,
    )

    # ==========================
    # CORS（Phase 3：改为可配置白名单）
    #   - allow_origins 含 * 时自动关闭 allow_credentials（浏览器规范要求）
    #   - 想恢复改造前的全开放行为：在 .env 中设置 CORS_ORIGINS=*
    # ==========================

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ==========================
    # 访问日志（Phase 3 新增，写入 logs/app.log）
    # ==========================

    @app.middleware("http")
    async def access_log(request: Request, call_next):

        started = time.perf_counter()

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - started) * 1000

        # 手机 / 平板上的页面会持续轮询这几个接口（AI 桥接 1.5 秒、入库进度 0.8~2.5 秒、
        # 续命 15 秒）。如果每条都写 INFO，启动器的可见控制台会被刷得看不清真正的错误，
        # 所以它们只写 DEBUG；其它请求仍然照常记 INFO。
        quiet = request.url.path.startswith(
            ("/ingest/status/", "/api/ai-bridge/pending", "/api/app/keepalive")
        )
        log = logger.debug if quiet else logger.info
        log(
            "%s %s -> %s %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms
        )

        return response

    # ==========================
    # 网页目录
    # ==========================

    app.mount(
        "/static",
        RevalidatingStaticFiles(directory=str(STATIC_DIR)),
        name="static"
    )

    # ==========================
    # 路由与统一错误处理
    # ==========================

    for router in all_routers:

        app.include_router(router)

    register_exception_handlers(app)

    logger.debug(
        "应用装配完成：CORS=%s 静态目录=%s",
        list(settings.cors_origins),
        STATIC_DIR
    )

    return app


app = create_app()