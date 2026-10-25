"""云端 OCR 提供方：PaddleOCR-VL 一步式作业接口。

协议（与官方 Python 示例一致）：
    POST {OCR_BASE_URL}                      multipart: file + model + optionalPayload
        headers: Authorization: bearer <OCR_TOKEN>
        → data.jobId
    GET  {OCR_BASE_URL}/{jobId}              → data.state: pending|running|done|failed
        data.extractProgress.{totalPages, extractedPages}
        data.resultUrl.jsonUrl              （state=done）
        data.errorMsg                        （state=failed）
    GET  {jsonUrl}                            JSONL：result.layoutParsingResults[*].markdown.text
        ⚠ 该地址通常是对象存储的预签名链接（签名在 URL 的 authorization 查询参数里），必须原样直连：
          不能附加 Authorization 头，否则对象存储会改按「请求头签名」校验、要求 date / x-bce-date，
          返回 400 MissingDateHeader（实测见 _result_headers）。

合规约束：**这是全项目唯一会把文件发送到本机之外的 OCR 实现**。
`OCR_MODE=off` 或 `OCR_PROVIDER≠paddle` 时上层根本不会调用它（零网络请求）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests

from app.core.config import get_settings
from app.services.ocr.base import (
    POLL_READ_TIMEOUT,
    RESULT_READ_TIMEOUT,
    RETRY_BACKOFF,
    SUBMIT_READ_TIMEOUT,
    SUBMIT_RETRIES,
    OcrOutcome,
    OcrProvider,
    brief,
    check_file_size,
    http_failure,
)

logger = logging.getLogger(__name__)

# 可选参数（与官方示例一致；默认为保守取值）
DEFAULT_OPTIONAL_PAYLOAD: Dict[str, bool] = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _auth_headers(settings) -> Dict[str, str]:
    return {
        "Authorization": f"bearer {settings.ocr_token}",
        "Accept": "application/json",
    }


def _host_of(url: str) -> str:
    """只取主机名用于日志：结果链接带签名，整条写进日志会泄漏。"""
    try:
        return urlsplit(url).netloc or "(unknown)"
    except ValueError:
        return "(unknown)"


def _result_headers(json_url: str, settings) -> Dict[str, str]:
    """下载结果用的请求头（**预签名链接绝不能带 Authorization 头**）。

    `resultUrl.jsonUrl` 通常是对象存储的预签名链接（百度 BOS 实测形如
    `.../json/xxx.json?authorization=bce-auth-v1%2F<AK>%2F<时间>%2F604800%2F<签名>`，有效期 7 天）。
    这类地址的签名就在 URL 里，必须原样直连：一旦再带上 `Authorization` 头，BOS 会改用
    「请求头签名」方式校验并要求 `date` / `x-bce-date`，于是返回 400 MissingDateHeader——
    作业其实已经 state=done，只是结果取不回来。
    因此只有结果地址与 `OCR_BASE_URL` 同源（将来若改回网关直出结果）时才附带令牌。
    """
    headers: Dict[str, str] = {"Accept": "application/json"}
    source = urlsplit(json_url).netloc.lower()
    service = urlsplit(getattr(settings, "ocr_base_url", "") or "").netloc.lower()
    if source and source == service:
        headers["Authorization"] = f"bearer {settings.ocr_token}"
    return headers


class PaddleJobsProvider(OcrProvider):
    """云端 PaddleOCR-VL（HTTP 作业接口）。"""

    name = "paddle"
    label = "云端 PaddleOCR-VL"
    external = True
    needs_token = True

    def available(self, settings) -> bool:
        return bool(getattr(settings, "ocr_enabled", False)) and bool(settings.ocr_token.strip())

    def unavailable_reason(self, settings) -> str:
        if settings.ocr_mode not in ("manual", "auto"):
            return "OCR 未启用（OCR_MODE=off）"
        if not settings.ocr_token.strip():
            return "未填写 OCR Token（设置页 → OCR 识别）"
        return "不可用"

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

        rejected = check_file_size(path, settings, provider=self.name, model=settings.ocr_model)
        if rejected is not None:
            return rejected

        job_id, error = self._submit(path, filename, content_type, settings)
        if not job_id:
            return self.fail(error, provider=self.name, model=settings.ocr_model, started=started)

        json_url, total_pages, error = self._poll(job_id, settings, started, on_progress)
        if not json_url:
            return self.fail(
                error,
                provider=self.name,
                model=settings.ocr_model,
                started=started,
                job_id=job_id,
                total_pages=total_pages,
            )

        pages, images, error = self._fetch_results(json_url, settings)
        if error:
            return self.fail(
                error,
                provider=self.name,
                model=settings.ocr_model,
                started=started,
                job_id=job_id,
                total_pages=total_pages,
            )

        text = "\n\n".join(pages)
        if not text.strip():
            return self.fail(
                "OCR 完成但未识别出任何文字（可能是空白页或纯图形内容）",
                provider=self.name,
                model=settings.ocr_model,
                started=started,
                job_id=job_id,
                total_pages=total_pages,
            )

        elapsed = time.monotonic() - started
        logger.info(
            "OCR 完成（云端）：job_id=%s 页数=%s/%s 字符=%s 用时=%.1fs",
            job_id,
            len(pages),
            total_pages or "?",
            len(text),
            elapsed,
        )
        return OcrOutcome(
            ok=True,
            text=text,
            pages=len(pages),
            total_pages=total_pages or len(pages),
            images=images,
            job_id=job_id,
            model=settings.ocr_model,
            provider=self.name,
            elapsed_s=elapsed,
            detail=f"OCR 识别完成（{len(pages)} 页，用时 {elapsed:.0f} 秒）",
        )

    # ---------- 内部步骤 ----------

    def _submit(self, path: Path, filename: str, content_type: str, settings) -> Tuple[str, str]:
        """提交作业（5xx/网络错误重试），返回 (jobId, 失败原因)。"""
        url = settings.ocr_base_url
        payload = {
            "model": settings.ocr_model,
            "optionalPayload": json.dumps(DEFAULT_OPTIONAL_PAYLOAD),
        }
        last_error = ""

        for attempt in range(SUBMIT_RETRIES + 1):
            retryable = True
            try:
                with path.open("rb") as handle:
                    response = _get_session().post(
                        url,
                        headers=_auth_headers(settings),
                        files={
                            "file": (
                                filename,
                                handle,
                                content_type or "application/octet-stream",
                            )
                        },
                        data=payload,
                        timeout=(settings.ocr_connect_timeout, SUBMIT_READ_TIMEOUT),
                    )
            except requests.exceptions.RequestException as exc:
                last_error = f"提交 OCR 作业失败（网络异常：{type(exc).__name__}）"
            else:
                if response.status_code < 400:
                    try:
                        job_id = str(
                            ((response.json() or {}).get("data") or {}).get("jobId") or ""
                        )
                    except ValueError:
                        job_id = ""
                    if job_id:
                        logger.info("OCR 作业已提交：job_id=%s file=%s", job_id, filename)
                        return job_id, ""
                    last_error = "OCR 服务未返回 jobId：" + brief(response.text)
                    retryable = False
                else:
                    retryable, last_error = http_failure(response.status_code, response.text)

            if not retryable or attempt >= SUBMIT_RETRIES:
                break
            time.sleep(RETRY_BACKOFF * (attempt + 1))

        return "", last_error

    def _poll(
        self, job_id: str, settings, started: float, on_progress=None
    ) -> Tuple[str, int, str]:
        """轮询作业状态，返回 (jsonUrl, totalPages, 失败原因)。"""
        url = f"{settings.ocr_base_url}/{job_id}"
        deadline = started + max(settings.ocr_timeout, 30)

        while True:
            if time.monotonic() > deadline:
                return "", 0, (
                    f"OCR 等待超过上限（{settings.ocr_timeout} 秒）仍未完成，可稍后在卡片上重试"
                )
            time.sleep(settings.ocr_poll_interval)

            try:
                response = _get_session().get(
                    url,
                    headers=_auth_headers(settings),
                    timeout=(settings.ocr_connect_timeout, POLL_READ_TIMEOUT),
                )
            except requests.exceptions.RequestException as exc:
                logger.warning("OCR 轮询网络异常（%s），继续重试：%s", type(exc).__name__, job_id)
                continue

            if response.status_code >= 400:
                retryable, detail = http_failure(response.status_code, response.text)
                if retryable:
                    logger.warning("OCR 轮询失败（可重试）：%s", detail)
                    continue
                return "", 0, detail

            try:
                data = (response.json() or {}).get("data") or {}
            except ValueError:
                continue

            state = str(data.get("state") or "").lower()
            progress = data.get("extractProgress") or {}
            total_pages = int(progress.get("totalPages") or 0)
            extracted = int(progress.get("extractedPages") or 0)
            if on_progress:
                on_progress(state, total_pages, extracted)

            if state == "done":
                json_url = str((data.get("resultUrl") or {}).get("jsonUrl") or "")
                if not json_url:
                    return "", total_pages, "OCR 已完成但未返回结果地址（resultUrl.jsonUrl 为空）"
                return json_url, total_pages, ""

            if state == "failed":
                reason = brief(str(data.get("errorMsg") or "服务未给出原因"))
                return "", total_pages, f"OCR 识别失败：{reason}"

    def _fetch_results(self, json_url: str, settings) -> Tuple[list, int, str]:
        """下载 JSONL 结果并抽取每页 Markdown，返回 (页文本列表, 插图数, 失败原因)。"""
        try:
            response = _get_session().get(
                json_url,
                headers=_result_headers(json_url, settings),
                timeout=(settings.ocr_connect_timeout, RESULT_READ_TIMEOUT),
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "OCR 结果下载网络异常（%s）：host=%s", type(exc).__name__, _host_of(json_url)
            )
            return [], 0, f"下载 OCR 结果失败（网络异常：{type(exc).__name__}）"

        if response.status_code >= 400:
            # 上游响应体必须带给用户：否则只剩「HTTP 400」，签名、过期还是参数都无从判断
            reason = brief(response.text)
            logger.warning(
                "OCR 结果下载失败：HTTP %s host=%s body=%s",
                response.status_code,
                _host_of(json_url),
                brief(response.text, 200),
            )
            return [], 0, f"下载 OCR 结果失败（HTTP {response.status_code}）：{reason}"

        # 结果体是 application/octet-stream（无 charset），显式按 UTF-8 解码，不靠猜
        response.encoding = "utf-8"

        pages: list = []
        images = 0
        for line in response.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            result = record.get("result") or {}
            for item in result.get("layoutParsingResults") or []:
                markdown = (item or {}).get("markdown") or {}
                text = str(markdown.get("text") or "").strip()
                if text:
                    pages.append(text)
                extra = markdown.get("images")
                if isinstance(extra, dict):
                    images += len(extra)

        return pages, images, ""


PROVIDER = PaddleJobsProvider()
