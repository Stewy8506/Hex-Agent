"""Resolve HEX_HOME for standalone skill scripts.

Skill scripts may run outside the HEX process (e.g. system Python,
nix env, CI) where ``hex_constants`` is not importable.  This module
provides the same ``get_hex_home()`` and ``display_hex_home()``
contracts as ``hex_constants`` without requiring it on ``sys.path``.

When ``hex_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``hex_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``HEX_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hex_constants import display_hex_home as display_hex_home
    from hex_constants import get_hex_home as get_hex_home
except (ModuleNotFoundError, ImportError):

    def get_hex_home() -> Path:
        """Return the HEX home directory (default: ~/.hex).

        Mirrors ``hex_constants.get_hex_home()``."""
        val = os.environ.get("HEX_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hex"

    def display_hex_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``hex_constants.display_hex_home()``."""
        home = get_hex_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
