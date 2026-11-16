"""Backward-compatibility re-export stub for gateways package.

Canonical implementation lives in harness_core.capabilities.gateways.
"""

from __future__ import annotations

import sys
from harness_core.capabilities.gateways import *  # noqa: F401, F403
import harness_core.capabilities.gateways as _impl

# Transparent module aliasing for mock / patch identity stability
sys.modules[__name__] = _impl
