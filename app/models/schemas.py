"""请求/响应模型（Phase 2：字段名与改造前保持一致）。"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Question(BaseModel):
    """POST /chat 请求体。

    改造前定义：class Question(BaseModel): message: str
    字段名 message 必须保持不变。
    """

    message: str
    #: P3F：前端在“本机已输出内容后失败”时手动换上游重试。
    #: 默认 False，不改变任何现有请求的行为；只对本次请求生效。
    force_upstream: bool = False
    #: 阶段 4 MVP：知识库无结果时，用户手动选择“不查资料继续”。
    #: 只对本次请求生效；走 LightRAG bypass + 会话历史，不检索知识库。
    force_bypass: bool = False
    #: M1：可选会话标识。旧客户端不传时行为不变；MEMORY_ENABLED=on 时服务端会
    #: 新建会话并通过 X-Session-Id 返回。client_message_id 用于重发/回退去重。
    session_id: str = Field(default="", max_length=128)
    client_message_id: str = Field(default="", max_length=128)
    topic_id: str = Field(default="", max_length=128)


class SessionCreate(BaseModel):
    """POST /api/sessions 请求体（M1：手动新建会话）。"""

    title: str = Field(default="", max_length=120)
    topic_id: str = Field(default="", max_length=128)


class TopicCreate(BaseModel):
    """POST /api/topics：创建资料集（记忆作用域）。"""

    name: str = Field(min_length=1, max_length=80)
    kind: Literal["project", "topic", "person", "other"] = "topic"
    description: str = Field(default="", max_length=500)
    parent_id: str = Field(default="", max_length=128)


class TopicUpdate(BaseModel):
    """PATCH /api/topics/{id}。"""

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[Literal["active", "archived"]] = None


class SessionUpdate(BaseModel):
    """PATCH /api/sessions/{id}：改标题/主题/归档。"""

    title: Optional[str] = Field(default=None, max_length=120)
    topic_id: Optional[str] = Field(default=None, max_length=128)
    status: Optional[str] = Field(default=None, max_length=16)


class SessionImport(BaseModel):
    """POST /api/sessions/import：把旧 localStorage 会话导入（幂等）。"""

    sessions: List[Dict[str, Any]] = []


MemoryKind = Literal[
    "preference", "goal", "fact", "constraint", "decision", "habit", "profile", "background"
]
MemorySensitivity = Literal["normal", "sensitive", "secret"]
MemorySourceType = Literal[
    "user_explicit", "user_stated", "user_confirmed", "model_inferred", "import"
]


class MemoryCreate(BaseModel):
    """POST /api/memory/items：手动创建一条长期记忆。"""

    content: str = Field(min_length=1, max_length=300)
    subject: str = Field(min_length=1, max_length=40)
    kind: MemoryKind = "fact"
    scope: Literal["global", "topic"] = "global"
    topic_id: str = Field(default="", max_length=128)
    importance: int = Field(default=3, ge=1, le=5)
    pinned: bool = False
    sensitivity: MemorySensitivity = "normal"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    status: Literal["active", "pending", "conflicted"] = "active"
    source_type: MemorySourceType = "user_confirmed"
    source_quote: str = Field(default="", max_length=500)
    source_message_id: str = Field(default="", max_length=128)
    source_session_id: str = Field(default="", max_length=128)
    meta: Dict[str, Any] = {}


class MemoryUpdate(BaseModel):
    """PATCH /api/memory/items/{id}：用户编辑。"""

    content: Optional[str] = Field(default=None, min_length=1, max_length=300)
    subject: Optional[str] = Field(default=None, min_length=1, max_length=40)
    kind: Optional[MemoryKind] = None
    scope: Optional[Literal["global", "topic"]] = None
    topic_id: Optional[str] = Field(default=None, max_length=128)
    importance: Optional[int] = Field(default=None, ge=1, le=5)
    pinned: Optional[bool] = None
    sensitivity: Optional[MemorySensitivity] = None
    valid_from: Optional[str] = Field(default=None, max_length=64)
    valid_to: Optional[str] = Field(default=None, max_length=64)
    meta: Optional[Dict[str, Any]] = None


class IngestAction(BaseModel):
    """POST /ingest/action/{file_id} 请求体（Phase 8 / Phase 15）。

    action 取值：ocr（云 OCR 解析）/ force（仍要入库）/ retry（重试处理）
    / ingest（确认入库）/ skip（跳过入库，Phase 15 的入库确认门）
    """

    action: str


class IngestSession(BaseModel):
    """POST /ingest/session 请求体（Phase 15）：「本次运行内全部允许」开关。

    allow_all=True 打开：本进程内新文件不再停在确认门，并把当前等确认的作业一并放行；
    关闭只影响后续文件。开关**只存在内存里、不落盘** —— 想永久关闭请到设置页
    把「入库确认方式」改成 off。
    """

    allow_all: bool = True


class IngestContent(BaseModel):
    """POST /ingest/content/{file_id} 请求体（Phase 15.2：先改正文再入库）。

    content 是**完整的正文**（不是补丁）：服务端会整篇写回 content.md，
    改之前的上一版留在 `data/parsed/<file_id>/content.before-edit.md`。
    ingest=False（默认）只保存 + 重新质检（仍停在确认门等你点「入库」）；
    ingest=True 表示「保存并入库」，等价于保存之后再点一次「入库」。

    Phase 17（图片资料）：caption=True 时 content 是**描述**（追加进正文，不覆盖已有
    正文），doc_name 是给这份资料起的名字（入库后成为上游文档名
    `<名字>_<短ID>.md`，短 ID 由服务端按内容指纹补，用户改不了）。
    """

    content: str = ""
    ingest: bool = False
    # Phase 17：图片资料的「写描述」——content 是描述、doc_name 是这份资料的名字
    caption: bool = False
    doc_name: str = ""


class ServiceControl(BaseModel):
    """服务控制请求体：POST /api/app/keepalive 、/api/app/leave 、/api/app/stop（Phase 10）。

    token 由 GET /api/app/control 下发（只有本机能读到，用来挡住局域网内的误调用）；
    delay 一般留空 —— 倒计时由后端按 CLOSE_STOP_DELAY / CLOSE_STOP_BUSY_DELAY 计算；
    reason 只用于日志（例如 tab-close / web-stop / lan-keepalive）。

    手机 / 局域网页面的 token 永远是空的（拿不到），它能调通的只有 keepalive，
    而 keepalive 只会把已预约的停止往后推 —— 停不掉任何东西（见 app/routers/control.py）。
    """

    token: str = ""
    delay: Optional[float] = None
    reason: str = ""



class FilesDeleteItem(BaseModel):
    """要删除的**一行**（Phase 16）：doc_id 与 file_id 属于同一行，必须成对给出。

    为什么单独建模型：以前只给 `doc_ids` 数组时，服务端拼不回「哪条知识库记录对应
    哪个本机作业」，只能退化成「删了知识库记录、本机留档没动」——
    用户点的是同一行，期望的是一起处理。所以页面按行提交 `items`，
    一对标识天然绑在一起，不需要任何猜测。
    """

    doc_id: str = ""       # 知识库里的文档号（可能为空：本机还没入库 / 已删除）
    file_id: str = ""      # 本机作业号（可能为空：别的工具传进知识库的文档）
    file_path: str = ""    # 知识库里的文档名（`<主干>.md`）
    name: str = ""         # 展示名（本机原件名优先）


class FilesDelete(BaseModel):
    """POST /api/files/delete 请求体（Phase 16：文件管理页的删除）。

    三种提交方式（页面用的是 `items`，另两种是简化调用时的便利形式）：
    · `items`     —— **一行一个对象**（doc_id / file_id / file_path 成对），页面用这个；
    · `doc_ids`   —— 只给知识库文档号：只删知识库记录，本机留档不动；
    · `file_ids`  —— 只给本机作业号：按文档名去上游反查 doc_id（翻不完就只清本机）。

    这**不是**宽松：真正动手的范围由 scope + remove_originals 决定，且默认都最保守：

    · token         控制令牌（只有本机页面拿得到，来自 GET /api/app/control）；
    · scope         删除范围三档：kb（只删知识库记录）/ local（+本机留档）/
                    all（+上游 input 里那份 md 拷贝）；
    · confirm       二次确认标志：false 直接 400（前端在弹窗里点「确认删除」才置 true）；
    · remove_originals 是否连**原件留档**（data\\originals、data\\uploads）一起删，默认否；
    · delete_llm_cache 是否顺带清 LLM 缓存（默认否；只有想彻底腾空间时才需要）。
    """

    token: str = ""
    items: List[FilesDeleteItem] = []
    doc_ids: List[str] = []
    file_ids: List[str] = []
    file_paths: List[str] = []
    scope: str = "kb"
    confirm: bool = False
    remove_originals: bool = False
    delete_llm_cache: bool = False
    reason: str = ""


class FolderOpen(BaseModel):
    """POST /api/files/open-folder 请求体（Phase 16）：在资源管理器里打开某个数据目录。

    target 取值见 app/routers/files.py 的 FOLDER_TARGETS（inbox / originals / uploads /
    knowledge / parsed / output / data）；只对**本机 + 令牌**开放，容器模式下明确 409。
    """

    token: str = ""
    target: str = "inbox"


class DocumentTopicUpdate(BaseModel):
    """POST /api/files/topic：给知识库文档打资料集标签（仅本机）。"""

    file_path: str = Field(min_length=1, max_length=500)
    topic_id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=200)
    file_id: str = Field(default="", max_length=128)


class InboxScan(BaseModel):
    """POST /api/inbox/scan 请求体（Phase 16）：立刻扫一次收件箱。

    平时靠后台线程按 INBOX_POLL_SECONDS 自动轮询；这个接口只是「现在就扫」，
    方便用户丢完文件立刻看到结果。只对**本机 + 令牌**开放。
    """

    token: str = ""
    reason: str = "manual"


