"""路由集合（Phase 2）。

注册顺序尽量贴近改造前 main.py 中的定义顺序，减少 /docs 展示顺序变化。
"""

from app.routers import (
    access,
    ai_bridge,
    chat,
    control,
    documents,
    files,
    graph,
    history,
    inbox,
    ingest,
    memory,
    sessions,
    settings,
    system,
    topics,
    tools,
    web,
)

all_routers = [
    web.router,        # GET  /web      （改造前定义在第 1 位）
    access.router,     # /pair、/access-required、局域网一键确认设备授权
    system.router,     # GET  / 、 /graph 、 /health
    chat.router,       # POST /chat
    ai_bridge.router,  # Batch 2：AHK → 本应用的内存中转（默认 off，不读写记忆）
    documents.router,  # POST /upload
    ingest.router,     # GET /ingest/status/{file_id} 、POST /ingest/action/{file_id}（Phase 8）
    sessions.router,   # M1：/api/sessions 会话与消息持久化（MEMORY_ENABLED=off 时只读返回 enabled=false）
    memory.router,     # M2：/api/memory 长期记忆管理（关闭时 enabled=false / 写接口 503）
    history.router,    # M4-mini：/api/history/search 只读历史检索（不写记忆、不自动注入）
    topics.router,     # 阶段 2B：资料集作用域（会话绑定、记忆隔离）
    settings.router,   # GET /settings 页面 与 /api/settings 配置接口
    control.router,    # GET /api/app/control 、POST /api/app/keepalive|leave|stop（Phase 10）
    graph.router,      # GET /api/graph/popular|search|subgraph（自建知识图谱页，只读，不代理任何写操作）
    files.router,      # GET /files 页面 与 /api/files/*（Phase 16 文档管理：列表 / 原件 / 打开文件夹 / 删除）
    inbox.router,      # GET /api/inbox/status 、POST /api/inbox/scan（Phase 16 文件夹轮询）
    tools.router,      # GET /api/tools（P1A 工具清单，只读，不探活）
]
