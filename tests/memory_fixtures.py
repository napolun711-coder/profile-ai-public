"""M1 测试夹具：临时 SQLite 库 + runtime 注入。"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Tuple

from app.memory import runtime
from app.memory.db import MemoryDatabase
from app.memory.store import MemoryStore


@contextmanager
def memory_store() -> Iterator[Tuple[MemoryStore, Path]]:
    with tempfile.TemporaryDirectory(prefix="profile-ai-memory-") as tmpdir:
        root = Path(tmpdir)
        store = MemoryStore(MemoryDatabase(root / "test.db"))
        store.initialize()
        runtime.reset_for_tests(store)
        try:
            yield store, root
        finally:
            runtime.reset_for_tests()


__all__ = ["memory_store"]
