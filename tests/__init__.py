r"""Offline tests.

Set a temporary profile home before importing application modules so tests never
write into the user's real %LOCALAPPDATA%\ProfileAI directory.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

TEST_PROFILE_HOME = tempfile.mkdtemp(prefix="profile-ai-tests-")
os.environ["PROFILE_AI_HOME"] = TEST_PROFILE_HOME
atexit.register(lambda: shutil.rmtree(TEST_PROFILE_HOME, ignore_errors=True))

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
