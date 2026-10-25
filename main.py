"""兼容入口（Phase 2 起）。

真实实现已迁移到 app/ 包内，本文件仅做转发，用于保持既有启动方式继续可用：

    uvicorn main:app --port 8100     # start.bat 使用的方式
    python main.py                   # 读取 .env 的 APP_HOST / APP_PORT
"""

from app.core.config import get_settings
from app.main import app

__all__ = ["app"]

settings = get_settings()


if __name__ == "__main__":

    import uvicorn

    from app.core.logging import setup_logging

    logger = setup_logging(settings.log_level)

    logger.info(
        "启动 本地知识库助手，当前配置：%s",
        settings.to_log_dict()
    )

    if not settings.quality_enabled:
        # Phase 18：质检是硬门，不看这个开关 —— 用户可能在 .env 里关过，
        # 静默忽略会让人以为"关掉了"，所以启动时明说一次（日志是静默模式的唯一排查入口）。
        logger.warning(
            "检测到 QUALITY_ENABLED=false，但质检是硬门、无法关闭：已忽略该设置，"
            "上传的文件仍会经过质量校验（未通过的只能在界面上点「仍要入库」放行）。"
        )

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port
    )