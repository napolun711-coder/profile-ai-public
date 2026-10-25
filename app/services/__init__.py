"""业务服务层（Phase 8）。

分层约定：`routers`（HTTP）→ `services`（业务流程）→ `clients`（外部服务）→ `core`（配置/日志/路径）。

- `parse.py`   本地解析：各格式 → 统一 Markdown
- `quality.py` 质量门：5 项指标 → pass / needs_ocr / blocked
- `ocr.py`     云 OCR（PaddleOCR-VL 一步式作业接口），默认关闭
- `ingest.py`  编排与状态落盘；**唯一**调用 `clients.lightrag.upload()` 的地方
- `library.py` 本机作业档案视图（Phase 16：知识库文档 ↔ 本机文件对上号，只读）
- `origin.py`  原件定位（Phase 16：双击参考资料时找到本机原件；只读，含路径白名单）
- `inbox.py`   文件夹轮询自动入库（Phase 16：`data\\inbox\\` 丢文件即入库，不直接碰知识库）
"""

__all__ = [
    "parse",
    "quality",
    "ocr",
    "ingest",
    "library",
    "origin",
    "inbox",
]
