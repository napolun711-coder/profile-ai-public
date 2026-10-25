"""P3A：本地 LLM 客户端单测（全部用假 session，不联网）。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, Dict, List, Optional

import requests

from app.clients import llm
from app.core import config


def cfg(**overrides: Any) -> config.Settings:
    base = config.Settings()
    values: Dict[str, Any] = {
        "llm_enabled": True,
        "llm_base_url": "http://localhost:11434",
        "llm_model": "qwen2.5:7b",
        "llm_timeout": 300,
        "llm_connect_timeout": 5,
        "llm_num_ctx": 8192,
        "llm_num_predict": 512,
    }
    values.update(overrides)
    return replace(base, **values)


class FakeResponse:
    def __init__(
        self,
        payload: Any = None,
        *,
        status_code: int = 200,
        json_error: bool = False,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self) -> Any:
        if self.json_error:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    def __init__(
        self,
        *,
        response: Optional[FakeResponse] = None,
        exc: Optional[Exception] = None,
    ) -> None:
        self.response = response
        self.exc = exc
        self.calls: List[Dict[str, Any]] = []

    def _call(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._call("POST", url, **kwargs)


def reply_payload(content: str = "答案 [1]") -> Dict[str, Any]:
    return {
        "model": "qwen2.5:7b",
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": 100,
        "eval_count": 20,
        "total_duration": 2_000_000_000,
        "load_duration": 500_000_000,
        "prompt_eval_duration": 700_000_000,
        "eval_duration": 800_000_000,
    }


MESSAGES = [
    {"role": "system", "content": "只依据资料回答"},
    {"role": "user", "content": "知识库里有哪些项目？"},
]


class DefaultsTests(unittest.TestCase):
    def test_local_generation_is_off_by_default(self) -> None:
        settings = config.Settings()

        self.assertFalse(settings.llm_enabled)
        self.assertEqual(settings.llm_base_url, "http://localhost:11434")
        self.assertEqual(settings.llm_model, "qwen2.5:7b")
        self.assertEqual(settings.llm_timeout, 300)
        self.assertEqual(settings.llm_num_ctx, 8192)
        self.assertEqual(settings.llm_num_predict, 512)


class GuardTests(unittest.TestCase):
    def test_disabled_refuses_without_request(self) -> None:
        session = FakeSession(response=FakeResponse(reply_payload()))

        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, settings=cfg(llm_enabled=False), session=session)

        self.assertEqual(caught.exception.kind, "llm_disabled")
        self.assertEqual(session.calls, [])

    def test_probe_disabled_refuses_without_request(self) -> None:
        session = FakeSession(response=FakeResponse({"models": []}))

        with self.assertRaises(llm.LLMError) as caught:
            llm.probe(settings=cfg(llm_enabled=False), session=session)

        self.assertEqual(caught.exception.kind, "llm_disabled")
        self.assertEqual(session.calls, [])

    def test_non_loopback_host_is_rejected(self) -> None:
        for url in (
            "http://192.168.1.5:11434",
            "http://10.0.0.8:11434",
            "https://example.com",
        ):
            with self.subTest(url=url):
                session = FakeSession(response=FakeResponse(reply_payload()))
                with self.assertRaises(llm.LLMError) as caught:
                    llm.chat(MESSAGES, settings=cfg(llm_base_url=url), session=session)
                self.assertEqual(caught.exception.kind, "llm_not_local")
                self.assertEqual(session.calls, [])

    def test_loopback_hosts_are_accepted(self) -> None:
        for url in (
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://[::1]:11434",
            "http://127.0.0.2:11434",
        ):
            with self.subTest(url=url):
                self.assertEqual(llm.normalize_base_url(url), url)

    def test_bad_base_urls_are_rejected(self) -> None:
        for url, kind in (
            ("ftp://localhost:11434", "llm_bad_base_url"),
            ("http://user:pass@localhost:11434", "llm_bad_base_url"),
            ("http://localhost:11434/ollama", "llm_bad_base_url"),
            ("", "llm_bad_base_url"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(llm.LLMError) as caught:
                    llm.normalize_base_url(url)
                self.assertEqual(caught.exception.kind, kind)


class ChatTests(unittest.TestCase):
    def test_request_shape_and_usage(self) -> None:
        session = FakeSession(response=FakeResponse(reply_payload()))

        reply = llm.chat(MESSAGES, settings=cfg(), session=session)

        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "http://localhost:11434/api/chat")
        body = call["json"]
        self.assertEqual(body["model"], "qwen2.5:7b")
        self.assertEqual(body["messages"], MESSAGES)
        self.assertFalse(body["stream"])
        self.assertEqual(body["options"]["temperature"], 0.0)
        self.assertEqual(body["options"]["seed"], 42)
        self.assertEqual(body["options"]["num_ctx"], 8192)
        self.assertEqual(body["options"]["num_predict"], 512)
        self.assertEqual(call["timeout"], (5, 300))

        self.assertEqual(reply.content, "答案 [1]")
        self.assertEqual(reply.model, "qwen2.5:7b")
        self.assertGreaterEqual(reply.elapsed_ms, 0.0)
        self.assertEqual(reply.usage["prompt_eval_count"], 100)
        self.assertEqual(reply.usage["eval_count"], 20)
        self.assertEqual(reply.usage["total_duration_ms"], 2000.0)

    def test_model_and_num_ctx_overrides(self) -> None:
        session = FakeSession(response=FakeResponse(reply_payload("好")))

        llm.chat(
            MESSAGES,
            model="qwen2.5:1.5b",
            num_ctx=4096,
            settings=cfg(),
            session=session,
        )

        body = session.calls[0]["json"]
        self.assertEqual(body["model"], "qwen2.5:1.5b")
        self.assertEqual(body["options"]["num_ctx"], 4096)

    def test_bad_messages_are_rejected_without_request(self) -> None:
        cases = (
            [],
            [{"role": "user", "content": ""}],
            [{"role": "robot", "content": "x"}],
            [{"role": "system", "content": "only system"}],
            ["not-an-object"],
        )
        for messages in cases:
            with self.subTest(messages=messages):
                session = FakeSession(response=FakeResponse(reply_payload()))
                with self.assertRaises(llm.LLMError) as caught:
                    llm.chat(messages, settings=cfg(), session=session)
                self.assertEqual(caught.exception.kind, "llm_bad_input")
                self.assertEqual(session.calls, [])

    def test_bad_num_ctx_is_rejected(self) -> None:
        session = FakeSession(response=FakeResponse(reply_payload()))
        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, num_ctx=128, settings=cfg(), session=session)
        self.assertEqual(caught.exception.kind, "llm_bad_input")
        self.assertEqual(session.calls, [])

    def test_empty_answer_is_rejected(self) -> None:
        session = FakeSession(response=FakeResponse(reply_payload("   ")))

        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, settings=cfg(), session=session)

        self.assertEqual(caught.exception.kind, "llm_bad_response")

    def test_malformed_json_is_rejected(self) -> None:
        session = FakeSession(response=FakeResponse(json_error=True))

        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, settings=cfg(), session=session)

        self.assertEqual(caught.exception.kind, "llm_bad_response")

    def test_http_error_does_not_leak_prompt(self) -> None:
        secret = "PROMPT-SECRET-123"
        session = FakeSession(response=FakeResponse({"error": "boom"}, status_code=500))

        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(
                [{"role": "user", "content": secret}],
                settings=cfg(),
                session=session,
            )

        self.assertEqual(caught.exception.kind, "llm_http_error")
        self.assertEqual(caught.exception.status_code, 500)
        self.assertNotIn(secret, caught.exception.message)

    def test_timeout_and_connection_errors(self) -> None:
        timeout = FakeSession(exc=requests.exceptions.Timeout("slow"))
        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, settings=cfg(), session=timeout)
        self.assertEqual(caught.exception.kind, "llm_timeout")

        refused = FakeSession(exc=requests.exceptions.ConnectionError("refused"))
        with self.assertRaises(llm.LLMError) as caught:
            llm.chat(MESSAGES, settings=cfg(), session=refused)
        self.assertEqual(caught.exception.kind, "llm_unavailable")


class ProbeTests(unittest.TestCase):
    def test_probe_reports_models_and_presence(self) -> None:
        payload = {"models": [{"name": "qwen2.5:7b"}, {"name": "bge-m3:latest"}]}
        session = FakeSession(response=FakeResponse(payload))

        result = llm.probe(settings=cfg(), session=session)

        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["url"], "http://localhost:11434/api/tags")
        self.assertEqual(result["models"], ["qwen2.5:7b", "bge-m3:latest"])
        self.assertTrue(result["model_present"])

    def test_probe_model_override_and_missing(self) -> None:
        session = FakeSession(response=FakeResponse({"models": [{"name": "qwen2.5:7b"}]}))

        result = llm.probe(settings=cfg(), session=session, model="qwen2.5:1.5b")

        self.assertFalse(result["model_present"])
        self.assertEqual(result["model"], "qwen2.5:1.5b")

    def test_probe_accepts_untagged_model_as_latest(self) -> None:
        session = FakeSession(response=FakeResponse({"models": [{"name": "qwen2.5:latest"}]}))

        result = llm.probe(settings=cfg(), session=session, model="qwen2.5")

        self.assertTrue(result["model_present"])

    def test_probe_http_error(self) -> None:
        session = FakeSession(response=FakeResponse({"error": "x"}, status_code=503))

        with self.assertRaises(llm.LLMError) as caught:
            llm.probe(settings=cfg(), session=session)

        self.assertEqual(caught.exception.kind, "llm_http_error")
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
