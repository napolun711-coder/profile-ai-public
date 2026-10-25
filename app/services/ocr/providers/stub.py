"""离线替身 OCR 提供方：不联网、不识别真实内容（仅用于单测与演示）。

用途：
- 单测里验证「图片 OCR 回填 → 复检 → 入库」全链路，不需要任何外部服务；
- 演示时可以看到界面流程，但识别结果是构造出来的，**不可用于真实资料**。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.config import get_settings
from app.services.ocr.base import OcrOutcome, OcrProvider

SAMPLE_LINES = (
    "（离线替身 OCR：以下文字为构造内容，不代表图片真实内容）",
    "示例：识别到的第一行文字",
    "示例：识别到的第二行文字",
)


class StubProvider(OcrProvider):
    """离线替身：永远成功，返回固定文本。"""

    name = "stub"
    label = "离线替身（仅测试用）"
    external = False
    needs_token = False

    def available(self, settings) -> bool:
        return settings.ocr_mode in ("manual", "auto")

    def unavailable_reason(self, settings) -> str:
        return "OCR 未启用（OCR_MODE=off）"

    def run(
        self,
        path: Path,
        filename: str,
        *,
        content_type: str = "",
        settings=None,
        on_progress=None,
    ) -> OcrOutcome:
        settings = settings or get_settings()
        started = time.monotonic()
        if not self.available(settings):
            return self.fail(self.unavailable_reason(settings), provider=self.name)

        body = "\n".join(list(SAMPLE_LINES) + [f"（来源文件：{Path(filename).name}）"])
        return OcrOutcome(
            ok=True,
            text=body,
            pages=1,
            total_pages=1,
            model="stub",
            provider=self.name,
            elapsed_s=time.monotonic() - started,
            detail="离线替身 OCR 完成（内容为构造，不用于真实资料）",
        )


PROVIDER = StubProvider()
