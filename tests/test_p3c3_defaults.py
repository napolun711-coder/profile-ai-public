"""P3C.3：默认模式锁定与本机模式保护的离线单测。

覆盖：
- 默认仍是 upstream；首 token 超时/并发上限的默认值与 env 解析；
- 首个内容 token 前超时 → `llm_first_token_timeout`（可回退上游）；
- 并发占满 → `local_busy`（可回退上游），释放后可再次生成；
- 启动自检只告警：模型缺失/探活失败记录规则化告警，不抛异常。
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest import mock

import requests

from app.clients import llm
from app.core.config import Settings, _build_settings
from app.services import local_generation


def local_settings(**overrides) -> Settings:
    values = dict(
        generation_mode="local",
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:11434",
        llm_model="qwen2.5:7b",
        llm_timeout=60,
        llm_connect_timeout=5,
    )
    values.update(overrides)
    return Settings(**values)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.status_code = 200
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def iter_lines(self):
        for item in self.payload:
            yield item


class FakeSession:
    def __init__(self, response) -> None:
        self.response = response

    def post(self, url, **kwargs):
        return self.response


class BlockingResponse(FakeResponse):
    """先给一个空内容帧，然后阻塞到看门狗关闭响应。"""

    def __init__(self) -> None:
        super().__init__([])
        self.release = threading.Event()

    def close(self) -> None:
        super().close()
        self.release.set()

    def iter_lines(self):
        yield json.dumps({"message": {"content": ""}}).encode()
        self.release.wait(5)
        raise requests.exceptions.ConnectionError("response closed")


class ConfigTests(unittest.TestCase):
    def test_defaults_lock_upstream_and_guards(self) -> None:
        settings = Settings()

        self.assertEqual(settings.generation_mode, "upstream")
        self.assertEqual(settings.llm_first_token_timeout, 120)
        self.assertEqual(settings.llm_max_concurrency, 1)

    def test_env_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_FIRST_TOKEN_TIMEOUT": "90",
                "LOCAL_LLM_MAX_CONCURRENCY": "2",
            },
            clear=False,
        ):
            settings = _build_settings()

        self.assertEqual(settings.llm_first_token_timeout, 90)
        self.assertEqual(settings.llm_max_concurrency, 2)

    def test_out_of_range_and_invalid_are_clamped(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_FIRST_TOKEN_TIMEOUT": "1",
                "LOCAL_LLM_MAX_CONCURRENCY": "0",
            },
            clear=False,
        ):
            settings = _build_settings()
        self.assertEqual(settings.llm_first_token_timeout, 5)
        self.assertEqual(settings.llm_max_concurrency, 1)

        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_FIRST_TOKEN_TIMEOUT": "9999",
                "LOCAL_LLM_MAX_CONCURRENCY": "99",
            },
            clear=False,
        ):
            settings = _build_settings()
        self.assertEqual(settings.llm_first_token_timeout, 600)
        self.assertEqual(settings.llm_max_concurrency, 8)

        with mock.patch.dict(
            os.environ,
            {
                "LOCAL_LLM_FIRST_TOKEN_TIMEOUT": "x",
                "LOCAL_LLM_MAX_CONCURRENCY": "x",
            },
            clear=False,
        ):
            settings = _build_settings()
        self.assertEqual(settings.llm_first_token_timeout, 120)
        self.assertEqual(settings.llm_max_concurrency, 1)

    def test_log_dict_exposes_guards_without_secrets(self) -> None:
        data = Settings().to_log_dict()

        self.assertEqual(data["llm_first_token_timeout"], 120)
        self.assertEqual(data["llm_max_concurrency"], 1)


class FirstTokenTimeoutTests(unittest.TestCase):
    def test_timeout_before_content_raises_safe_error(self) -> None:
        response = BlockingResponse()
        with self.assertRaises(llm.LLMError) as caught:
            list(
                llm.stream_chat(
                    [{"role": "user", "content": "问题"}],
                    settings=local_settings(),
                    session=FakeSession(response),
                    first_token_timeout=0.3,
                )
            )

        self.assertEqual(caught.exception.kind, "llm_first_token_timeout")
        self.assertTrue(response.closed)
        self.assertNotIn("问题", caught.exception.message)

    def test_content_before_timeout_still_streams(self) -> None:
        response = FakeResponse(
            [
                json.dumps({"message": {"content": "你"}}).encode(),
                json.dumps({"done": True, "eval_count": 1}).encode(),
            ]
        )
        chunks = list(
            llm.stream_chat(
                [{"role": "user", "content": "问题"}],
                settings=local_settings(),
                session=FakeSession(response),
                first_token_timeout=0.3,
            )
        )

        self.assertEqual([item.content for item in chunks], ["你", ""])
        self.assertTrue(chunks[-1].done)


class ConcurrencyTests(unittest.TestCase):
    def test_busy_slot_falls_back_and_recovers(self) -> None:
        cfg = Settings(generation_mode="local", llm_max_concurrency=1)

        with local_generation._generation_slot(cfg):
            with self.assertRaises(local_generation.LocalGenerationError) as caught:
                list(local_generation.iter_stream("问题", settings=cfg))
        self.assertEqual(caught.exception.kind, "local_busy")
        self.assertFalse(caught.exception.streamed)

        # 释放后可以再次占用（不会永久卡死）。
        with local_generation._generation_slot(cfg):
            pass

    def test_generation_slot_is_per_limit(self) -> None:
        cfg = Settings(generation_mode="local", llm_max_concurrency=2)

        with local_generation._generation_slot(cfg):
            with local_generation._generation_slot(cfg):
                with self.assertRaises(local_generation.LocalGenerationError):
                    with local_generation._generation_slot(cfg):
                        pass


class StartupCheckTests(unittest.TestCase):
    def test_missing_model_logs_warning_only(self) -> None:
        from app import main

        with mock.patch.object(
            local_generation, "generation_mode", return_value="local"
        ), mock.patch.object(
            local_generation,
            "probe_model",
            return_value={"model_present": False, "model": "qwen2.5:7b"},
        ):
            with self.assertLogs("app.main", level="WARNING") as logs:
                main._warn_if_local_unavailable()

        text = "\n".join(logs.output)
        self.assertIn("未提供模型", text)
        self.assertIn("qwen2.5:7b", text)

    def test_probe_failure_logs_warning_only(self) -> None:
        from app import main

        with mock.patch.object(
            local_generation, "generation_mode", return_value="local"
        ), mock.patch.object(
            local_generation, "probe_model", side_effect=RuntimeError("down")
        ):
            with self.assertLogs("app.main", level="WARNING") as logs:
                main._warn_if_local_unavailable()

        self.assertIn("自检失败", "\n".join(logs.output))

    def test_upstream_mode_skips_probe(self) -> None:
        from app import main

        with mock.patch.object(
            local_generation, "generation_mode", return_value="upstream"
        ), mock.patch.object(local_generation, "probe_model") as probe:
            main._warn_if_local_unavailable()

        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
