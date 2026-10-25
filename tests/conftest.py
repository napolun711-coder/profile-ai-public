"""测试环境隔离：不要让本地 `.env` 的 M4/本机生成开关影响离线单测。"""

from __future__ import annotations

import os

# 真实运行时可能启用本机 7B / M4；单测必须默认走 upstream + 记忆副作用关闭，
# 需要测这些能力的用例自己在 Settings 里覆盖。
for _key, _value in {
    "GENERATION_MODE": "upstream",
    "MEMORY_ENABLED": "off",
    "MEMORY_CONTEXT_ENABLED": "off",
    "MEMORY_RECALL_ENABLED": "off",
    "MEMORY_FAST_MEMORY": "off",
    "MEMORY_AUTO_EXTRACT": "off",
    "MEMORY_WORKER": "off",
    "MEMORY_UPSTREAM_HISTORY": "off",
    "MEMORY_UPSTREAM_MEMORY": "off",
}.items():
    os.environ.setdefault(_key, _value)
