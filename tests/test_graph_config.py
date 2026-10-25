"""图谱节点上限使用实际配置，而不是页面写死 400。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.routers import graph


class GraphConfigTests(unittest.TestCase):
    def test_default_global_graph_uses_configured_capacity(self) -> None:
        cfg = replace(get_settings(), graph_max_nodes=3500)
        with mock.patch.object(
            graph, "get_settings", return_value=cfg
        ), mock.patch.object(
            graph.lightrag, "graph_global", return_value={"nodes": [], "edges": []}
        ) as load:
            graph.global_graph(0)
        load.assert_called_once_with(3500)

    def test_explicit_graph_limit_still_wins(self) -> None:
        cfg = replace(get_settings(), graph_max_nodes=3500)
        with mock.patch.object(
            graph, "get_settings", return_value=cfg
        ), mock.patch.object(
            graph.lightrag, "graph_global", return_value={"nodes": [], "edges": []}
        ) as load:
            graph.global_graph(800)
        load.assert_called_once_with(800)


if __name__ == "__main__":
    unittest.main()
