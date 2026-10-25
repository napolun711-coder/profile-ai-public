"""知识图谱只读接口（自建图谱页用；**刻意不代理任何写操作**）。

背景（为什么不再嵌 LightRAG 自带的 WebUI）：
那个界面里有上传（绕过本项目的质量门）、删除文档、清空知识库、重建索引，
Knowledge Graph 页里甚至能**删除实体/关系**。个人用户误操后无法自行恢复，只能运维介入。
所以图谱改成自建：只暴露"看图"需要的三个只读接口，用户根本到不了那些操作。

  GET /api/graph/popular?limit=        热门实体（图谱页默认视图）
  GET /api/graph/search?q=&limit=      模糊搜索实体（search node in page）
  GET /api/graph/global?max_nodes=     整张知识图谱（图谱页默认视图）
  GET /api/graph/subgraph?label=&depth=&max_nodes=   以某实体为中心的子图

节点的 properties 自带 `file_path`（来源文档）与 `description`，
因此「从节点跳到相关文档 / 就这个实体提问」在前端即可完成，无需再开接口。
"""

from typing import Any, Dict

from fastapi import APIRouter, Query

from app.clients import lightrag
from app.core.config import get_settings

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/popular")
def popular(
    limit: int = Query(30, ge=1, le=100, description="返回多少个热门实体"),
) -> Dict[str, Any]:
    return {"items": lightrag.graph_popular(limit)}


@router.get("/search")
def search(
    q: str = Query(..., min_length=1, max_length=64, description="实体名关键词（模糊匹配）"),
    limit: int = Query(20, ge=1, le=50),
) -> Dict[str, Any]:
    return {"items": lightrag.graph_search(q, limit)}


@router.get("/global")
def global_graph(
    max_nodes: int = Query(
        0,
        ge=0,
        le=10000,
        description="最多返回多少个节点；0 表示按 GRAPH_MAX_NODES 配置",
    ),
) -> Dict[str, Any]:
    """整张知识图谱 —— 图谱页的**默认视图**（上游用 label="*" 取全图）。"""
    effective = int(max_nodes or get_settings().graph_max_nodes)
    payload = lightrag.graph_global(effective)
    if isinstance(payload, dict):
        payload["requested_max_nodes"] = effective
    return payload


@router.get("/subgraph")
def subgraph(
    label: str = Query(..., min_length=1, max_length=128, description="中心实体名"),
    depth: int = Query(2, ge=1, le=3, description="展开几跳"),
    max_nodes: int = Query(60, ge=10, le=200, description="最多多少个节点（防止图过大）"),
) -> Dict[str, Any]:
    # 上游返回 {nodes, edges, is_truncated}，原样透出；is_truncated=true 由前端提示"图太大已截断"
    return lightrag.graph_subgraph(label, depth, max_nodes)
