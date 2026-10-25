"""Port reservation and runtime endpoint selection."""

from __future__ import annotations

import unittest
from unittest import mock

from app.core import runtime


class RuntimeTests(unittest.TestCase):
    def test_parse_and_match_excluded_ranges(self) -> None:
        text = "Protocol tcp Port Exclusion Ranges\n 9607 9706\n 18000 18010\n"
        ranges = runtime.parse_excluded_ranges(text)
        self.assertEqual(ranges, ((9607, 9706), (18000, 18010)))
        self.assertTrue(runtime.port_is_excluded(9621, ranges))
        self.assertFalse(runtime.port_is_excluded(9961, ranges))

    def test_candidate_ports_skip_app_port(self) -> None:
        ports = runtime.candidate_ports(9621, 18021)
        self.assertNotIn(18021, ports)
        self.assertEqual(ports[0], 9621)

    def test_reserved_9621_falls_back_and_is_persisted(self) -> None:
        chosen = {}
        with mock.patch.object(runtime, "load_runtime", return_value={}), \
             mock.patch.object(runtime, "windows_excluded_ranges", return_value=((9607, 9706),)), \
             mock.patch.object(runtime, "port_open", return_value=False), \
             mock.patch.object(runtime, "port_bindable", side_effect=lambda host, port: port == 18021), \
             mock.patch.object(runtime, "save_runtime", side_effect=lambda values: chosen.update(values)):
            url, changed = runtime.resolve_lightrag_endpoint("http://127.0.0.1:9621", 8100, prefer_persisted=True)
        self.assertTrue(changed)
        self.assertEqual(url, "http://127.0.0.1:18021")
        self.assertEqual(chosen["lightrag_port"], 18021)

    def test_healthy_existing_endpoint_is_reused(self) -> None:
        chosen = {}
        with mock.patch.object(runtime, "load_runtime", return_value={}), \
             mock.patch.object(runtime, "windows_excluded_ranges", return_value=((9607, 9706),)), \
             mock.patch.object(runtime, "port_open", side_effect=lambda host, port: port == 9621), \
             mock.patch.object(runtime, "lightrag_health", return_value=True), \
             mock.patch.object(runtime, "save_runtime", side_effect=lambda values: chosen.update(values)):
            url, changed = runtime.resolve_lightrag_endpoint("http://127.0.0.1:9621", 8100, prefer_persisted=True)
        self.assertFalse(changed)
        self.assertEqual(url, "http://127.0.0.1:9621")
        self.assertEqual(chosen["lightrag_port"], 9621)

    def test_remote_endpoint_is_not_rewritten(self) -> None:
        url, changed = runtime.resolve_lightrag_endpoint("https://rag.example.com", 8100)
        self.assertEqual(url, "https://rag.example.com")
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
