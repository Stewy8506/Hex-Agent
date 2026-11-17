"""Custom Autonomous AI Harness Core Package."""

from __future__ import annotations

import importlib
from typing import Any

# Single source of truth is pyproject.toml; installed distributions expose it
# via package metadata. The fallback must match pyproject's version.
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("hex-ai")
except Exception:
    __version__ = "0.22.0"

_NAMESPACES: dict[str, str] = {
    "capabilities": "harness_core.capabilities",
    "client": "harness_core.client",
    "cognition": "harness_core.cognition",
    "core": "harness_core.core",
    "models": "harness_core.models",
    "runtime": "harness_core.runtime",
    "storage": "harness_core.storage",
}

_ROOT_SYMBOLS: dict[str, tuple[str, str]] = {
    "HarnessOrchestrator": ("harness_core.core.orchestrator", "HarnessOrchestrator"),
    "LedgerStore": ("harness_core.storage.db", "LedgerStore"),
    "TwoTierModelRouter": ("harness_core.core.router", "TwoTierModelRouter"),
    "ToolGateway": ("harness_core.gateway", "ToolGateway"),
}

__all__ = [
    "__version__",
    "capabilities",
    "client",
    "cognition",
    "core",
    "models",
    "runtime",
    "storage",
    "HarnessOrchestrator",
    "LedgerStore",
    "TwoTierModelRouter",
    "ToolGateway",
]


def __getattr__(name: str) -> Any:
    if name in _NAMESPACES:
        mod = importlib.import_module(_NAMESPACES[name])
        globals()[name] = mod
        return mod
    if name in _ROOT_SYMBOLS:
        mod_name, attr_name = _ROOT_SYMBOLS[name]
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return list(__all__)
