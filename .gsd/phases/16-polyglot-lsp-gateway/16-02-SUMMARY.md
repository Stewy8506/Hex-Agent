# Plan 16.02: Tool Gateway Registration, Autonomous Repair Pre-Flight Interceptor & CLI/TUI Ergonomics — Summary

**Phase:** 16 (Polyglot LSP Gateway & Compiler Diagnostic Interceptor)  
**Plan:** 16-02  
**Status:** Completed  
**Commits:** `3a36aab`, `075d29f`, `2f57cd0`  
**Tests:** 13/13 in `tests/test_lsp_gateway_and_repair.py`, 25/25 total LSP tests (480/480 full test suite green, 0 regressions)

---

## 1. Objectives & Executive Summary

Plan 16-02 exposed Language Server Protocol (LSP) capabilities across all user and agent interfaces in the HEX Autonomous AI Workbench, integrating compiler diagnostics directly into the Tool Gateway, the multi-turn autonomous repair loop, the CLI (`hex lsp`), and the interactive TUI (`/lsp`).

Key Deliverables:
1. **ToolGateway R1 LSP Tools (`src/harness_core/gateway.py`):**
   - Registered 4 read-only R1 tools eligible for fast-path express lane and deterministic 0ms call-level journaling:
     * `lsp_diagnostics`: Queries compiler syntax errors, type mismatches, and linter warnings with severity filtering (`error`, `warning`, `all`).
     * `lsp_definition`: Resolves symbol definition locations to file URI and 1-based line/column coordinates.
     * `lsp_references`: Identifies all reference call sites and declaration points across workspace.
     * `lsp_hover`: Fetches markdown docstrings and function/variable type signatures at coordinates.
   - Built graceful fallback returning clean `{status: "no_server_available", language: ..., diagnostics: []}` structures when language server binaries are not installed.
   - Enhanced `LSPManager` (`src/harness_core/lsp/manager.py`) with workspace-level `get_definition`, `get_references`, and `get_hover` helpers.

2. **Pre-Flight Compiler Diagnostic Interceptor (`src/harness_core/orchestrator.py`):**
   - Implemented `_run_compiler_preflight(attempt_ws, modified_files)` in `HarnessOrchestrator` to synchronize modified workspace files with language servers via `did_open`/`did_change` and poll for errors.
   - Integrated pre-flight gate into `_execute_standard_dag` both after initial DAG execution and after each autonomous repair patch application.
   - Rejects patches and DAG steps with fatal syntax/compiler errors before running slow test suites, recording cryptographic hash-chained `LSP_COMPILER_PREFLIGHT_FAILED` task events in `LedgerStore`.
   - Directly synthesizes readable compiler diagnostic errors (`[file:line:col] [ERROR] code: message`) into `failure_digest` for the next repair attempt, saving minutes of failed test execution time.

3. **CLI & TUI Ergonomics (`src/harness_core/cli.py`, `src/harness_core/tui.py`):**
   - Added `hex lsp` CLI subcommand supporting:
     * `hex lsp status [--workspace <path>] [--json]`: Displays detected/installed server status matrix across workspace.
     * `hex lsp diagnostics [--file <path>] [--errors-only] [--json]`: Queries workspace or single-file diagnostics.
     * `hex lsp definition <file> <line> <col>`: Finds symbol definition.
     * `hex lsp hover <file> <line> <col>`: Prints type signature and docstrings.
   - Added `/lsp` interactive slash command to `HexConsole`:
     * `/lsp status`: Renders styled language server table.
     * `/lsp diagnostics [file]`: Displays error/warning tallies and colorized line diagnostics.
     * `/lsp`: Displays high-level language server matrix and top compiler errors.
     * Updated `/help` command catalog and capability splash cards with `lsp_compiler` category.

4. **Integration Test Suite (`tests/test_lsp_gateway_and_repair.py`):**
   - Authored 13 end-to-end integration tests verifying tool invocation through `ToolGateway`, compiler pre-flight interceptor short-circuiting test runner, ledger event recording, CLI invocations, and TUI slash commands.

---

## 2. Tasks Completed

| Task | Name | Files | Commits |
|------|------|-------|---------|
| 1 | Register Read-Only LSP Tools in ToolGateway | `src/harness_core/gateway.py`, `src/harness_core/lsp/manager.py` | `3a36aab` |
| 2 | Integrate Pre-Flight Compiler Diagnostic Interceptor into Autonomous Repair Loop | `src/harness_core/orchestrator.py`, `src/harness_core/db.py` | `075d29f` |
| 3 | Implement CLI Commands, TUI `/lsp` Slash Command & Comprehensive Integration Test Suite | `src/harness_core/cli.py`, `src/harness_core/gateway.py`, `src/harness_core/tui.py`, `tests/test_lsp_gateway_and_repair.py` | `2f57cd0` |

---

## 3. Verification & Empirical Proof

1. **ToolGateway LSP Registration Verification:**
   ```bash
   PYTHONPATH=src .venv/bin/python -c "from harness_core.gateway import ToolGateway; from harness_core.db import LedgerStore; gw = ToolGateway(LedgerStore(':memory:')); print({t: gw.tools[t].risk_class.value for t in gw.tools if t.startswith('lsp_')})"
   # {'lsp_diagnostics': 'R1', 'lsp_definition': 'R1', 'lsp_references': 'R1', 'lsp_hover': 'R1'}
   ```
2. **Orchestrator Pre-Flight Interceptor Method Verification:**
   ```bash
   PYTHONPATH=src .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator(':memory:'); print(hasattr(orch, '_run_compiler_preflight'))"
   # True
   ```
3. **CLI `hex lsp status` Execution:**
   ```bash
   PYTHONPATH=src .venv/bin/python -m harness_core.cli lsp status
   # Polyglot Language Server Registry (Harness):
   # LANGUAGE     SERVER                     STATUS         EXTENSIONS
   # python *     pyright                    [NOT FOUND]    .py, .pyi
   # rust         rust-analyzer              [INSTALLED]    .rs
   # cpp          clangd                     [INSTALLED]    .c, .cpp, .cc, .cxx, .h, .hpp, .hxx
   ```
4. **Integration Test Suite (`tests/test_lsp_gateway_and_repair.py`):**
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest tests/test_lsp_gateway_and_repair.py -v
   # Ran 13 tests in 4.810s - OK (13 passed, 0 failed)
   ```
5. **Full Repository Regression Suite:**
   ```bash
   PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
   # Ran 480 tests in 127.814s - OK (480 passed, 0 failures, 0 errors, 0 regressions)
   ```

---

## 4. State & Next Steps

Phase 16 (Polyglot LSP Gateway & Compiler Diagnostic Interceptor) is now 100% complete across both plans (16-01 and 16-02).
All 480 unit, integration, and contract tests in the repository pass cleanly with zero regressions.
Ready for Phase 17 (Real-Time Hands-Free Voice & Speech Pair-Programming).
