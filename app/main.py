"""应用装配（Phase 2 建立；Phase 3 接入配置化 CORS、访问日志与统一错误处理；
Phase 15 增加启动时的「待确认作业」超时归档；Phase 16 增加文件夹轮询线程）。"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import __version__
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


def _timed_startup_step(name: str, action) -> None:
    """运行一个可降级的启动维护步骤，并记录耗时。"""
    started = time.perf_counter()
    try:
        action()
    except Exception:  # noqa: BLE001 - 维护任务失败不能阻止应用接收请求
        LOGGER.exception("启动维护失败（已降级）：%s", name)
    else:
        LOGGER.info(
            "启动维护完成：%s（%.0f ms）",
            name,
            (time.perf_counter() - started) * 1000,
        )


def _startup_maintenance(stop: threading.Event) -> None:
    """执行不参与 HTTP 就绪判定的维护任务。

    FastAPI 只有在 lifespan 让出控制权后才会接收请求。收件箱扫描、历史归档、
    记忆库修复和模型探测都可以降级，不能因为磁盘、杀毒扫描或外部服务变慢而让
    网页端口迟迟不开放。这里放在独立 daemon 线程中执行，主应用立即就绪。
    """
    LOGGER.info("应用 HTTP 已就绪；后台维护开始")
    if stop.is_set():
        return

    def archive_expired() -> None:
        archived = ingest_service.archive_expired_reviews()
        if archived:
            LOGGER.info("启动归档：%d 个等待确认超时的作业已标记为跳过", len(archived))

    _timed_startup_step("归档超时待确认作业", archive_expired)

    if stop.is_set():
        return

    def start_inbox() -> None:
        if inbox_service.start():
            LOGGER.info("文件夹轮询已启动（收件箱：data\\inbox）")

    _timed_startup_step("启动文件夹轮询", start_inbox)

    if stop.is_set():
        return

    def initialize_memory() -> None:
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

    _timed_startup_step("初始化对话持久化", initialize_memory)

    if stop.is_set():
        return

    def start_memory_worker() -> None:
        from app.memory.extract import register_extraction_handler
        from app.memory.worker import get_worker

        worker = get_worker()
        register_extraction_handler(worker)
        worker.start()

    _timed_startup_step("启动记忆 worker", start_memory_worker)

    if stop.is_set():
        return
    _timed_startup_step("本机生成自检", _warn_if_local_unavailable)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """快速开放 HTTP，再在后台完成可降级的维护任务。"""
    ensure_directories()
    stop = threading.Event()
    maintenance = threading.Thread(
        target=_startup_maintenance,
        args=(stop,),
        name="startup-maintenance",
        daemon=True,
    )
    maintenance.start()

    try:
        yield
    finally:
        stop.set()
        maintenance.join(timeout=2.0)
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
        version=__version__,
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
