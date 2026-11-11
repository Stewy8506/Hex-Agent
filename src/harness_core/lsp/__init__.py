"""Polyglot Language Server Protocol (LSP) Gateway for HEX.

Provides zero-dependency JSON-RPC 2.0 stdio transport, multi-language
server auto-detection, real-time compiler diagnostics interception, and
symbol navigation.
"""

from harness_core.lsp.client import (
    LSPMessage,
    LSPResponseError,
    LSPTimeoutError,
    StdioLSPTransport,
)

__all__ = [
    "LSPMessage",
    "LSPResponseError",
    "LSPTimeoutError",
    "StdioLSPTransport",
]
