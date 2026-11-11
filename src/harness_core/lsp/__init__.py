"""Polyglot Language Server Protocol (LSP) Gateway for HEX.

Provides zero-dependency JSON-RPC 2.0 stdio transport, multi-language
server auto-detection, real-time compiler diagnostics interception, and
symbol navigation.
"""

from harness_core.lsp.client import (
    LSPClient,
    LSPDiagnostic,
    LSPMessage,
    LSPResponseError,
    LSPTimeoutError,
    StdioLSPTransport,
    path_to_uri,
    uri_to_path,
)
from harness_core.lsp.manager import (
    LSPManager,
)
from harness_core.lsp.registry import (
    LanguageServerRegistry,
    LanguageServerSpec,
)

__all__ = [
    "LSPClient",
    "LSPDiagnostic",
    "LSPManager",
    "LSPMessage",
    "LSPResponseError",
    "LSPTimeoutError",
    "LanguageServerRegistry",
    "LanguageServerSpec",
    "StdioLSPTransport",
    "path_to_uri",
    "uri_to_path",
]
