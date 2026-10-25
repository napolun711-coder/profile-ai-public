"""P2F：`/chat` 的生产技能运行时（默认关闭，按需懒加载）。

边界：
- 通用知识技能只在 `SKILL_ROUTING=on` 时构造；
- 只注册三个只读工具：`knowledge.search` / `knowledge.answer` / `image.search`；
  `knowledge.answer` 与 `image.search` 仍不进入 `BUILTIN_TOOLS`；
- P3B：`GENERATION_MODE=local` 时改用本机生成版 `knowledge.answer`
  （工具名/策略不变），失败仍走原有回退；
- P3C：`stream_answer()` 用工作线程 + 队列转发工具的 emit 增量；
  路由、预算、审计仍由原 Manager/ToolManager 完成，客户端断开时置位取消；
- `image_lookup` 仍不进入默认 `BUILTIN_SKILLS`，只在这里显式注册；
- Skill 抛异常、返回 error、答案为空、图片技能没有证据时，本模块返回 `None`，
  由 `/chat` 回退到上游直通；
- 不记录问题、答案、证据正文；日志只留异常类型，工具审计只留元数据。

这是一层“生产适配器”，不修改 SkillManager/ToolManager 的既有语义。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.plugins.adapters import image as image_adapter
from app.plugins.adapters import knowledge as knowledge_adapter
from app.plugins.audit import AuditLogger
from app.plugins.manager import ToolManager, ToolPolicy
from app.plugins.registry import ToolRegistry
from app.services import image_evidence
from app.services.context_builder import Citation
from app.skills.builtin import image_lookup, knowledge_qa, register_builtin_skills
from app.skills.contracts import SkillInput, SkillOutput
from app.skills.manager import SkillManager
from app.skills.registry import SkillRegistry
from app.skills.router import SkillRouter

logger = logging.getLogger(__name__)

ROUTING_ON = "on"
ROUTING_OFF = "off"
ROUTING_VALUES = (ROUTING_OFF, ROUTING_ON)

#: 技能路径只允许这三个只读工具；多一个都不放开。
ENABLED_TOOLS = (
    knowledge_adapter.TOOL.spec.name,
    knowledge_adapter.ANSWER_TOOL.spec.name,
    image_adapter.TOOL.spec.name,
)

FALLBACK_SKILL = knowledge_qa.SKILL_NAME


@dataclass(frozen=True)
class SkillChatAnswer:
    """一次技能问答的“可回给 /chat”的最小结果；不携带证据正文。"""

    response: str
    references: Tuple[Dict[str, Any], ...]
    response_time: float
    skill: str = ""
    status: str = ""
    #: P3C.2：内容校验结果；None 表示非本机路径，未做内容校验。
    verified: Optional[bool] = None
    warnings: Tuple[str, ...] = ()
    #: 仅本机工具会带用量与 done_reason；上游路径为空。
    usage: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """映射成 `/chat` 现有字段：response / references / response_time。"""
        return {
            "response": self.response,
            "references": [dict(item) for item in self.references],
            "response_time": self.response_time,
        }


class SkillRuntime:
    """对 Manager + ToolManager 的一次生产包装；无状态，可被多个请求共享。"""

    def __init__(
        self,
        manager: SkillManager,
        tool_manager: Optional[ToolManager] = None,
    ) -> None:
        self.manager = manager
        self.tool_manager = tool_manager

    def answer(
        self, message: str, *, options: Optional[Dict[str, Any]] = None
    ) -> Optional[SkillChatAnswer]:
        """执行一次技能问答；不可用/失败返回 None，让调用方回退上游。"""
        if not isinstance(message, str) or not message.strip():
            return None

        started = time.perf_counter()
        try:
            output = self.manager.run(
                SkillInput(message, options=dict(options or {}))
            )
        except Exception as exc:  # noqa: BLE001 - 生产入口不能把异常抛给 /chat
            logger.warning("技能运行时异常，回退上游：%s", type(exc).__name__)
            return None

        return self._to_answer(output, started)

    def stream_answer(
        self, message: str, *, options: Optional[Dict[str, Any]] = None
    ):
        """流式技能入口：转发工具 emit 的 references/chunk，最后给出内部 `_answer`。

        - 只有 `emit` 被真正传到底层工具时才会有增量；图片技能仍整段返回；
        - 客户端断开时生成器 close → `cancelled` 置位 → 本机流式请求停止；
        - 审计、策略、路由仍在原 Manager/ToolManager 内完成，不另起一套。
        """
        if not isinstance(message, str) or not message.strip():
            return

        events: "queue.Queue" = queue.Queue()
        cancelled = threading.Event()

        def emit(event) -> None:
            if cancelled.is_set():
                return
            if isinstance(event, dict):
                events.put(("event", event))

        def worker() -> None:
            started = time.perf_counter()
            try:
                output = self.manager.run(
                    SkillInput(message, options=dict(options or {})),
                    emit=emit,
                    cancelled=cancelled.is_set,
                )
                events.put(("output", (output, started)))
            except Exception as exc:  # noqa: BLE001 - 生产入口不能把异常抛给 HTTP
                events.put(("exception", type(exc).__name__))
            finally:
                events.put(("stop", None))

        threading.Thread(
            target=worker, name="skill-stream", daemon=True
        ).start()

        try:
            while True:
                kind, payload = events.get()
                if kind == "stop":
                    break
                if kind == "event":
                    if payload.get("type") in ("references", "chunk"):
                        yield payload
                    continue
                if kind == "output":
                    output, started = payload
                    answer = self._to_answer(output, started)
                    message_text = ""
                    if (
                        answer is None
                        and isinstance(output, SkillOutput)
                        and output.error is not None
                    ):
                        message_text = output.error.message
                    yield {
                        "type": "_answer",
                        "answer": answer,
                        "error": message_text,
                    }
                    continue
                if kind == "exception":
                    yield {
                        "type": "_answer",
                        "answer": None,
                        "error": "技能流式执行异常",
                    }
        finally:
            cancelled.set()

    def _to_answer(self, output: Any, started: float) -> Optional[SkillChatAnswer]:
        if not self._usable(output):
            return None
        trace = output.trace if isinstance(output.trace, dict) else {}
        usage = output.usage if isinstance(output.usage, dict) else {}
        warnings = usage.get("content_warning_codes") or ()
        if not isinstance(warnings, (list, tuple)):
            warnings = ()
        return SkillChatAnswer(
            response=output.answer,
            references=tuple(references_from_output(output)),
            response_time=_elapsed_seconds(trace, started),
            skill=str(trace.get("skill") or ""),
            status=str(output.status or ""),
            verified=(
                usage["verified"]
                if isinstance(usage.get("verified"), bool)
                else None
            ),
            warnings=tuple(
                str(code) for code in warnings if isinstance(code, str)
            )[:10],
            usage={
                str(key): value
                for key, value in usage.items()
                if key
                in (
                    "done_reason",
                    "prompt_eval_count",
                    "eval_count",
                    "total_duration_ms",
                    "load_duration_ms",
                    "prompt_eval_duration_ms",
                    "eval_duration_ms",
                )
            },
        )

    def close(self) -> None:
        if self.tool_manager is not None:
            self.tool_manager.close()

    @staticmethod
    def _usable(output: Any) -> bool:
        if not isinstance(output, SkillOutput):
            return False
        if output.status == "error":
            return False
        if not isinstance(output.answer, str) or not output.answer.strip():
            return False

        trace = output.trace if isinstance(output.trace, dict) else {}
        skill = str(trace.get("skill") or "")
        # 图片问题没有找到图片时，上游文本回答可能更有用；不用“没有找到”覆盖它。
        if skill == image_lookup.SKILL_NAME and not output.evidence:
            return False
        return True


def references_from_output(output: SkillOutput) -> List[Dict[str, Any]]:
    """把技能引用映射成前端认识的 `reference_id` / `file_path`。

    刻意不返回 evidence 正文、caption、OCR 预览或 asset_paths；
    图片只额外带 `kind/label/file_id/image_id`，供 P2G 的前端渲染使用。
    """
    references: List[Dict[str, Any]] = []
    for citation in output.citations:
        if not isinstance(citation, Citation):
            continue
        file_path = str(citation.doc_name or "").strip()
        reference_id = str(citation.reference_id or "").strip()
        if not file_path and not reference_id:
            continue

        item: Dict[str, Any] = {
            "reference_id": reference_id,
            "file_path": file_path,
        }
        if citation.kind == image_evidence.IMAGE_KIND:
            item["kind"] = citation.kind
            item["label"] = citation.label
            if citation.file_id:
                item["file_id"] = citation.file_id
            if citation.image_id:
                item["image_id"] = citation.image_id
        references.append(item)
    return references


def _answer_tool():
    """按 `GENERATION_MODE` 选择回答工具；任何异常都回退上游版。"""
    try:
        mode = str(get_settings().generation_mode or "").strip().lower()
    except Exception:  # noqa: BLE001 - 配置坏了仍要能构造运行时
        mode = "upstream"
    if mode != "local":
        return knowledge_adapter.ANSWER_TOOL
    try:
        # 延迟导入：upstream 模式不加载本机生成门面与 Ollama 客户端。
        from app.plugins.adapters import local_answer

        return local_answer.TOOL
    except Exception as exc:  # noqa: BLE001 - 本机工具加载失败不能拖垮 /chat
        logger.warning("本机回答工具加载失败，回退上游工具：%s", type(exc).__name__)
        return knowledge_adapter.ANSWER_TOOL


def build_runtime(*, audit: Optional[AuditLogger] = None) -> SkillRuntime:
    """构造一次生产运行时；只在显式调用时加载工具与技能。"""
    tools = ToolRegistry()
    tools.register(knowledge_adapter.TOOL)
    tools.register(_answer_tool())
    tools.register(image_adapter.TOOL)

    policy = ToolPolicy(
        allowed_external=True,
        enabled_tools=ENABLED_TOOLS,
        allowed_side_effects=("none", "read_local"),
    )
    tool_manager = ToolManager(
        tools,
        policy=policy,
        audit=audit if audit is not None else AuditLogger(),
    )

    skills = SkillRegistry()
    register_builtin_skills(skills)          # knowledge_qa
    skills.register(image_lookup.SKILL)      # opt-in，只在这里注册
    router = SkillRouter(skills, fallback=FALLBACK_SKILL)
    manager = SkillManager(
        skills,
        tools=tool_manager,
        router=router,
        fallback=FALLBACK_SKILL,
    )
    return SkillRuntime(manager, tool_manager)


_lock = threading.Lock()
_runtime: Optional[SkillRuntime] = None
_build_failed = False


def skill_routing_enabled() -> bool:
    """读取启动时冻结的配置；非法/读取异常一律按关闭处理。"""
    try:
        return str(get_settings().skill_routing or "").strip().lower() == ROUTING_ON
    except Exception:  # noqa: BLE001 - 配置坏了也不能拖垮 /chat
        return False


def get_runtime() -> Optional[SkillRuntime]:
    """懒加载单例；开关关闭或构建失败时返回 None（调用方回退上游）。"""
    global _runtime, _build_failed

    if not skill_routing_enabled():
        return None
    if _runtime is not None:
        return _runtime
    if _build_failed:
        return None

    with _lock:
        if _runtime is not None:
            return _runtime
        if _build_failed:
            return None
        try:
            _runtime = build_runtime()
        except Exception as exc:  # noqa: BLE001 - 生产入口必须可回退
            _build_failed = True
            logger.warning("技能运行时初始化失败，/chat 回退上游：%s", type(exc).__name__)
            return None
    return _runtime


def reset_runtime() -> None:
    """释放单例（测试/热重载用；正常请求不会调用）。"""
    global _runtime, _build_failed
    with _lock:
        if _runtime is not None:
            _runtime.close()
        _runtime = None
        _build_failed = False


def _elapsed_seconds(trace: Dict[str, Any], started: float) -> float:
    value = trace.get("elapsed_ms")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
    ):
        return round(float(value) / 1000.0, 3)
    return round(max(0.0, time.perf_counter() - started), 3)


__all__ = [
    "ENABLED_TOOLS",
    "FALLBACK_SKILL",
    "ROUTING_OFF",
    "ROUTING_ON",
    "ROUTING_VALUES",
    "SkillChatAnswer",
    "SkillRuntime",
    "build_runtime",
    "get_runtime",
    "references_from_output",
    "reset_runtime",
    "skill_routing_enabled",
]
