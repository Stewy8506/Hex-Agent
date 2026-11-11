# Plan 16.01: Polyglot LSP Client Core & Stdio Transport — Summary

**Phase:** 16 (Polyglot LSP Gateway & Compiler Diagnostic Interceptor)  
**Plan:** 16-01  
**Status:** Completed  
**Commits:** `938bad5`, `299b163`, `4299249`  
**Tests:** 12/12 in `tests/test_lsp_client.py` (467/467 full test suite green, 0 regressions)

---

## 1. Objectives & Executive Summary

Plan 16-01 delivered the core engine for HEX's zero-dependency Polyglot Language Server Protocol (LSP) Gateway, establishing compiler awareness across major programming languages.

Key Deliverables:
1. **JSON-RPC 2.0 Base Protocol & Stdio Transport (`src/harness_core/lsp/client.py`):**
   - Implemented `StdioLSPTransport` using standard HTTP-style `Content-Length: <n>\r\n\r\n` framing without external dependencies.
   - Built background reader thread parsing headers and body streams, demultiplexing request IDs against pending wait events.
   - Handled server-to-client requests (e.g. `workspace/configuration`) with default safe responses to avoid deadlocks.
   - Added asynchronous stderr logger thread with rolling 100-line diagnostic buffer and clean process teardown with timeout and SIGKILL fallback.
   - Defined `LSPMessage`, `LSPResponseError`, `LSPTimeoutError`, and `LSPDiagnostic`.
2. **Multi-Language Server Registry & Auto-Detection (`src/harness_core/lsp/registry.py`):**
   - Defined `LanguageServerSpec` and `LanguageServerRegistry` supporting 6 major language families:
     * **Python:** `pyright`, `ruff`, `pylsp`
     * **TypeScript / JavaScript:** `typescript-language-server`, `vtsls`
     * **Go:** `gopls`
     * **Rust:** `rust-analyzer`
     * **C / C++:** `clangd`
     * **Java:** `jdtls`
   - Added executable auto-detection via `shutil.which` and environment variable overrides (`HEX_LSP_PYTHON_BIN`, `HEX_LSP_TS_BIN`, `HEX_LSP_<SERVER>_BIN`).
   - Implemented workspace server detection matching project root markers and cross-referencing with `detect_recipe`.
3. **LSP Client Lifecycle & Document Synchronization (`src/harness_core/lsp/client.py`):**
   - Implemented `LSPClient` managing the standard `initialize` handshake and `initialized` notification with client capabilities.
   - Implemented document sync: `did_open`, `did_change`, and `did_close`.
   - Built symbol query methods: `get_hover`, `get_definition`, `get_references`.
   - Subscribed to `textDocument/publishDiagnostics`, parsing diagnostics into structured `LSPDiagnostic` models with severity normalization (ERROR, WARNING, INFO, HINT).
4. **LSP Manager Workspace Coordinator (`src/harness_core/lsp/manager.py`):**
   - Implemented `LSPManager` managing per-language active `LSPClient` instances with lazy instantiation on first file access.
   - Provided aggregate diagnostics retrieval (`get_diagnostics`) across active clients or filtered by file/severity.
   - Implemented graceful batch shutdown (`close_all`) and context manager lifecycle (`__enter__` / `__exit__`).
5. **Empirical Verification Test Suite (`tests/test_lsp_client.py`):**
   - Authored 12 unit tests verifying framing, mock stdio server communication, document sync, published diagnostics, definition/hover/reference queries, registry detection, and manager coordination.

---

## 2. Tasks Completed

| Task | Name | Files | Commits |
|------|------|-------|---------|
| 1 | Implement JSON-RPC 2.0 Base Protocol & Stdio Transport with Content-Length Framing | `src/harness_core/lsp/__init__.py`, `src/harness_core/lsp/client.py` | `938bad5` |
| 2 | Implement Language Server Registry & Multi-Language Auto-Detection | `src/harness_core/lsp/registry.py`, `src/harness_core/lsp/__init__.py` | `299b163` |
| 3 | Implement LSP Client Lifecycle, Document Synchronization, Diagnostic Store & Unit Tests | `src/harness_core/lsp/client.py`, `src/harness_core/lsp/manager.py`, `src/harness_core/lsp/__init__.py`, `tests/test_lsp_client.py` | `4299249` |

---

## 3. Verification & Empirical Proof

1. **Task 1 Verification:**
   ```bash
   PYTHONPATH=src python3 -c "from harness_core.lsp.client import StdioLSPTransport, LSPMessage, LSPResponseError; print('LSP transport module imported successfully')"
   # LSP transport module imported successfully (Exit code 0)
   ```
2. **Task 2 Verification:**
   ```bash
   PYTHONPATH=src python3 -c "from harness_core.lsp.registry import LanguageServerRegistry; reg = LanguageServerRegistry(); print('Registered language servers:', len(reg.get_all_specs()))"
   # Registered language servers: 9 (Exit code 0)
   ```
3. **Task 3 Unit Test Suite (`tests/test_lsp_client.py`):**
   ```bash
   .venv/bin/python -m unittest tests/test_lsp_client.py -v
   # Ran 12 tests in 1.051s - OK (12 passed, 0 failed)
   ```
4. **Full Repository Regression Suite:**
   ```bash
   .venv/bin/python -m unittest discover -s tests
   # Ran 467 tests in 110.855s - OK (467 passed, 0 failures, 0 errors, 0 regressions)
   ```

---

## 4. State & Next Steps

All 3 tasks in Plan 16-01 are complete and verified with atomic commits.
Ready for Plan 16-02 (Tool Gateway Integration, Autonomous Repair Interception & CLI/TUI Ergonomics).
