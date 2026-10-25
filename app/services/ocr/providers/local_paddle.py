"""本机 OCR 提供方：PaddleOCR（可选依赖，默认不安装）。

特点：
- **不出网**：文件不离开本机（`external=False`），适合隐私敏感资料；
- 可选依赖：没装 `paddleocr` 时不会让程序崩溃，只给出可操作的中文提示；
- 当前只处理图片；PDF 请用云端提供方（或先转成图片）。

安装（可选）：
    pip install "paddleocr>=2.7" "paddlepaddle>=2.5"
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

from app.core.config import get_settings
from app.services.ocr.base import IMAGE_SUFFIXES, OcrOutcome, OcrProvider, check_file_size

logger = logging.getLogger(__name__)

INSTALL_HINT = (
    '未安装本机 OCR 依赖：请执行 pip install "paddleocr"（或用 Python 3.12/3.13 环境 '
    "uv sync --extra ocr-local）。注意 PaddleOCR 目前尚未支持 Python 3.14，"
    "也可先把 OCR_PROVIDER 改为 paddle 使用云端识别"
)


class LocalPaddleProvider(OcrProvider):
    """本机 PaddleOCR（CPU/GPU 均可，默认 CPU）。"""

    name = "local"
    label = "本机 PaddleOCR（不外发）"
    external = False
    needs_token = False

    _engine: Any = None
    _engine_key: str = ""
    _engine_error: str = ""

    def available(self, settings) -> bool:
        return settings.ocr_mode in ("manual", "auto") and bool(
            getattr(settings, "ocr_enabled", False)
        )

    def unavailable_reason(self, settings) -> str:
        if settings.ocr_mode not in ("manual", "auto"):
            return "OCR 未启用（OCR_MODE=off）"
        return "本机 OCR 不可用"

    # ---------- 内部 ----------

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

        suffix = Path(filename or path.name).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            return self.fail(
                f"本机 OCR 仅支持图片（{suffix or '未知格式'}）："
                "PDF 请把页面导出为图片后上传，或把 OCR_PROVIDER 改为 paddle 使用云端识别",
                provider=self.name,
            )

        rejected = check_file_size(path, settings, provider=self.name, model="paddleocr")
        if rejected is not None:
            return rejected

        engine, error = self._load(settings)
        if engine is None:
            return self.fail(error, provider=self.name)

        try:
            raw = engine.ocr(str(path), cls=True)
        except Exception as exc:  # noqa: BLE001 - 识别失败一律转成可展示原因
            logger.warning("本机 OCR 失败：%s", exc)
            return self.fail(
                f"本机 OCR 识别失败（{type(exc).__name__}）：{exc}",
                provider=self.name,
                started=started,
            )

        lines, scores = _extract_lines(raw)
        text = "\n".join(lines).strip()
        elapsed = time.monotonic() - started
        if not text:
            return self.fail(
                "本机 OCR 未识别出任何文字（可能是空白图或纯图形内容）",
                provider=self.name,
                started=started,
            )

        logger.info(
            "OCR 完成（本机）：file=%s 行数=%s 字符=%s 用时=%.1fs",
            filename,
            len(lines),
            len(text),
            elapsed,
        )
        average = (sum(scores) / len(scores)) if scores else 0.0
        return OcrOutcome(
            ok=True,
            text=text,
            pages=1,
            total_pages=1,
            model="paddleocr",
            provider=self.name,
            elapsed_s=elapsed,
            detail=f"本机 OCR 完成（{len(lines)} 行，平均置信度 {average:.2f}）",
        )

    def _load(self, settings) -> Tuple[Any, str]:
        """惰性加载识别引擎（按 lang/device/模型目录缓存）。"""
        key = f"{settings.ocr_lang}|{settings.ocr_device}|{settings.ocr_local_model_dir}"
        if self._engine is not None and self._engine_key == key:
            return self._engine, ""
        if self._engine_error and self._engine_key == key:
            return None, self._engine_error

        try:
            from paddleocr import PaddleOCR  # type: ignore
        except ImportError:
            self._engine_key = key
            self._engine_error = INSTALL_HINT
            return None, self._engine_error

        kwargs: dict = {"lang": settings.ocr_lang or "ch"}
        if settings.ocr_device:
            kwargs["device"] = settings.ocr_device
        if settings.ocr_local_model_dir:
            kwargs["det_model_dir"] = settings.ocr_local_model_dir

        try:
            self._engine = PaddleOCR(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 模型加载失败给出可操作提示
            self._engine_key = key
            self._engine_error = f"本机 OCR 初始化失败（{type(exc).__name__}）：{exc}"
            return None, self._engine_error

        self._engine_key = key
        self._engine_error = ""
        return self._engine, ""


def _extract_lines(raw: Any) -> Tuple[List[str], List[float]]:
    """把 PaddleOCR 的多种返回结构统一成 (文本行, 置信度)。

    PaddleOCR 2.x：[[ [box, (text, score)], ... ]]
    PaddleOCR 3.x：返回 dict（rec_texts / rec_scores），这里做兼容扫描。
    """
    lines: List[str] = []
    scores: List[float] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            for key in ("rec_texts", "texts", "text"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    lines.append(value.strip())
                elif isinstance(value, list):
                    lines.extend(
                        item.strip() for item in value if isinstance(item, str) and item.strip()
                    )
            for key in ("rec_scores", "scores"):
                value = node.get(key)
                if isinstance(value, list):
                    scores.extend(
                        float(item) for item in value if isinstance(item, (int, float))
                    )
            for value in node.values():
                if isinstance(value, (list, dict)):
                    walk(value)
            return
        if isinstance(node, (list, tuple)):
            if len(node) == 2 and isinstance(node[0], str) and isinstance(node[1], (int, float)):
                if node[0].strip():
                    lines.append(node[0].strip())
                    scores.append(float(node[1]))
                return
            for item in node:
                walk(item)

    walk(raw)
    return lines, scores


PROVIDER = LocalPaddleProvider()
