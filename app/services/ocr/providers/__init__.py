"""OCR 提供方集合（Phase 9）：paddle（云端）/ local（本机）/ stub（离线替身）。"""

from app.services.ocr.providers import local_paddle, paddle_jobs, stub

__all__ = ["local_paddle", "paddle_jobs", "stub"]
