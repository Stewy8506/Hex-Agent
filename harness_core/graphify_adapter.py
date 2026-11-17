"""Compatibility module aliasing ActiveGraphifyAdapter and symbol utilities."""

from .graphify import (
    ActiveGraphifyAdapter,
    SymbolDef,
    GraphifyUpstreamRunner,
    get_god_nodes,
    detect_languages_in_workspace,
)

__all__ = [
    "ActiveGraphifyAdapter",
    "SymbolDef",
    "GraphifyUpstreamRunner",
    "get_god_nodes",
    "detect_languages_in_workspace",
]
