"""P3A：Prompt 注册表骨架（L0–L4 分层，纯函数）。

分层：
    L0 身份      —— 本机知识库助手
    L1 安全隐私  —— 只依据资料、不编造、资料不足要说清楚
    L2 证据引用  —— 只使用资料中原有的方括号编号
    L3 输出格式  —— 先结论后依据，中文
    L4 用户问题  —— 由 `build_messages()` 放进 user 消息；不拼进 system prompt

边界：
- 纯函数：不读文件、不发请求、不读配置；
- 相同输入必须得到完全相同的输出（不做随机选择、不按时间变化）；
- `context_text` 与用户问题只进入 user 消息，**不进入 system prompt**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

PROMPT_VERSION = "p3a.1"

USER_LAYER = "L4"


@dataclass(frozen=True)
class PromptLayer:
    layer_id: str
    title: str
    text: str


LAYERS: Tuple[PromptLayer, ...] = (
    PromptLayer(
        "L0",
        "身份",
        "你是运行在用户本机的知识库助手，服务对象是用户本人。",
    ),
    PromptLayer(
        "L1",
        "安全与隐私",
        "只使用本轮提供的资料回答；不要编造资料中没有的事实。"
        "资料不足时明确说明「资料中没有足够信息」，并指出缺少什么。"
        "不要复述与本轮任务无关的隐私信息。",
    ),
    PromptLayer(
        "L2",
        "证据与引用",
        "资料以「[编号] 文件名」开头，同一编号可能对应同一文档的多段内容。"
        "引用时必须原样使用资料中出现的编号；不要发明新编号，也不要把不同编号混用。"
        "每个来自资料的要点末尾都要标注对应的 [编号]；没有资料支撑的句子不要写。",
    ),
    PromptLayer(
        "L3",
        "回答格式",
        "先用一两句话给出结论，再列出依据；每条依据末尾用 [编号] 标注来源。使用中文回答。",
    ),
)


def build_system_prompt(*, output_format: str = "") -> str:
    """按 L0→L3 顺序拼接 system prompt；`output_format` 非空时追加在最后。"""
    parts = [layer.text for layer in LAYERS]
    extra = str(output_format or "").strip()
    if extra:
        parts.append("输出格式要求：\n" + extra)
    return "\n\n".join(parts)


def _clean_history(
    history: Optional[Sequence[Mapping[str, object]]],
) -> List[Dict[str, str]]:
    """只保留 user/assistant 的非空历史；最多 40 条，防止坏输入撑爆上下文。"""
    if not isinstance(history, (list, tuple)):
        return []
    cleaned: List[Dict[str, str]] = []
    for item in history[-40:]:
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content})
    return cleaned


def build_messages(
    *,
    question: str,
    context_text: str = "",
    system_prompt: Optional[str] = None,
    output_format: str = "",
    history: Optional[Sequence[Mapping[str, object]]] = None,
    prefix_text: str = "",
) -> List[Dict[str, str]]:
    """构造 Ollama `messages`：system（L0–L3）+ 历史 + user（记忆/资料 + 问题）。

    `history` 与 `prefix_text` 默认空，旧调用输出完全不变。
    `prefix_text`（长期记忆/使用规则）只进入 user 消息，不进入 system。
    """
    text = str(question or "").strip()
    if not text:
        raise ValueError("question 不能为空")

    if system_prompt is None:
        system = build_system_prompt(output_format=output_format)
    else:
        system = str(system_prompt or "").strip()
        if not system:
            raise ValueError("system_prompt 不能为空")

    prefix = str(prefix_text or "").strip()
    context = str(context_text or "").strip()
    if prefix and context:
        user = f"{prefix}\n\n以下是与问题相关的资料：\n\n{context}\n\n问题：{text}"
    elif prefix:
        user = f"{prefix}\n\n（本轮没有可用资料）\n\n问题：{text}"
    elif context:
        user = f"以下是与问题相关的资料：\n\n{context}\n\n问题：{text}"
    else:
        user = f"（本轮没有可用资料）\n\n问题：{text}"

    messages = [{"role": "system", "content": system}]
    messages.extend(_clean_history(history))
    messages.append({"role": "user", "content": user})
    return messages


def layer_map() -> Mapping[str, PromptLayer]:
    """只读视图：layer_id → PromptLayer（便于测试/脚本按层检查）。"""
    return {layer.layer_id: layer for layer in LAYERS}
