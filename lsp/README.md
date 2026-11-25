# `lsp` (`harness_core.lsp`)

**Zero-dependency polyglot Language Server Protocol (LSP) stdio client, server registry, and workspace manager.**

`lsp/` maps to `harness_core.lsp` in [`pyproject.toml`](../pyproject.toml). It provides compiler-accurate diagnostics, go-to-definition, find-references, and hover types over JSON-RPC 2.0 `Content-Length` stdio transport without third-party LSP dependencies.

## Modules

| File | Purpose |
| :--- | :--- |
| `__init__.py` | Exports `LSPClient`, `LSPDiagnostic`, `LSPManager`, `LSPMessage`, `LSPResponseError`, `LSPTimeoutError`, `LanguageServerRegistry`, `LanguageServerSpec`, `StdioLSPTransport`, `path_to_uri`, `uri_to_path`. |
| `client.py` | `StdioLSPTransport` (`Content-Length` framing + reader thread) and `LSPClient` (`initialize`, `did_open`, `did_change`, `did_close`, `get_definition`, `get_references`, `get_hover`, `get_diagnostics`, `shutdown`). |
| `registry.py` | `LanguageServerSpec` (resolves `HEX_LSP_<SERVER>_BIN`, `HEX_LSP_<LANG>_BIN`, then `PATH`) and `LanguageServerRegistry` (`find_server_for_file`, `detect_workspace_servers` cross-referencing `detect_recipe`). |
| `manager.py` | `LSPManager`: lazily spawns and caches one `LSPClient` per language, syncs workspace edits, and exposes 1-based line/column lookups (`sync_file`, `get_diagnostics`, `get_definition`, `get_references`, `get_hover`, `close_all`). |

## Supported Language Servers (9 Specs / 6 Languages)

| Language ID | Configured Servers (Priority Order) |
| :--- | :--- |
| `python` | `pyright` (`pyright-langserver`/`pyright`, 100), `ruff` (`ruff server`, 90), `pylsp` (80) |
| `typescript` | `typescript-language-server` (100), `vtsls` (90) |
| `go` | `gopls` (100) |
| `rust` | `rust-analyzer` (100) |
| `cpp` | `clangd` (100) |
| `java` | `jdtls` (100) |

## Integration & Tests

- Exposed via `ToolGateway` (`lsp_diagnostics`, `lsp_definition`, `lsp_references`, `lsp_hover`), `agent/orchestrator.py` (`_run_compiler_preflight`), `hex lsp` CLI (`hex_cli/cli.py`), and `/lsp` / `/compiler` slash commands (`hex_cli/tui.py`, `hex_cli/acp.py`).
- Tests live in [`tests/lsp/`](../tests/lsp).
