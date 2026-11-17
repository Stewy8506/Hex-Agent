"""Backward-compatibility re-export stub for Discord Bot Gateway.

Canonical implementation lives in harness_core.capabilities.gateways.discord_bot.
"""

from __future__ import annotations

import sys
from harness_core.capabilities.gateways.discord_bot import *  # noqa: F401, F403
import harness_core.capabilities.gateways.discord_bot as _impl

# Transparent module aliasing for mock / patch identity stability
sys.modules[__name__] = _impl
