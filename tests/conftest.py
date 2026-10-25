"""Pytest compatibility.

The actual isolation lives in tests/__init__.py so it also applies to
`python -m unittest discover`.
"""

from tests import TEST_PROFILE_HOME  # noqa: F401
