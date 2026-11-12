"""Resolve HEX_HOME for standalone skill scripts.

Skill scripts may run outside the HEX process (system Python, nix env,
CI) where ``hex_constants`` is not importable.  This module provides the
same ``get_hex_home()`` contract without requiring it on ``sys.path``.

When ``hex_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hex_constants import get_hex_home as get_hex_home
except (ModuleNotFoundError, ImportError):

    def get_hex_home() -> Path:
        """Return the HEX home directory (default: ``~/.hex``)."""
        val = os.environ.get("HEX_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hex"
