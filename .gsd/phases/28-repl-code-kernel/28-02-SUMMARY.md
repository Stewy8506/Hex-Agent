# Plan 28-02 Summary: Gateway Tool & System Integration (ToolGateway, ReActLoop, TUI /kernel)

## Executive Summary
Plan 28-02 integrated the stateful, persistent Python REPL engine into HEX's central runtime components:
1. **ToolGateway**: Registered `code_kernel` as an authoritative built-in tool under `RiskClass.R2`, providing programmatic tool execution (`execute_tool`), strict secret redaction on all streams (stdout, stderr, traceback), automatic stale module warning generation when on-disk Python source files change after import, and full policy enforcement (denied on fast-path tasks).
2. **ReActLoop**: Wired session ownership (`session_id`) into multi-turn ReAct loops so consecutive reasoning turns retain variable and function definitions across turns, with clean presentation of observations and warnings back to the model.
3. **HexConsole TUI**: Implemented developer-facing `/kernel` (and `/repl`) slash commands (`status`, `reset`, `vars`, `exec <code>`), complete with Rich table renderings, ANSI fallbacks, autocompletion, and help documentation.
4. **Integration Testing**: Created a complete 14-test integration suite in `tests/test_gateway_code_kernel.py` covering state retention, reset, redaction, stale module detection, policy gating, multi-turn ReAct loops, and TUI slash commands. Updated `test_gateway_tools.py` and `test_acp_server.py` to reflect 31 registered gateway tools with zero regressions.

---

## Tasks Completed

| Task | Description | Status | Commit |
|------|-------------|--------|--------|
| Task 1 | ToolGateway code_kernel Registration & ReActLoop Integration | Complete | `e8030b4` |
| Task 2 | HexConsole /kernel Slash Command & End-to-End Integration Suite | Complete | `6bdc9c9` |

---

## Key Changes & Implementations

### 1. ToolGateway (`src/harness_core/gateway.py`)
- Registered `code_kernel` schema in `TOOL_DEFINITIONS` with `risk_class: R2` and parameters `code`, `reset`, and `timeout`.
- Added `kernel_registry: KernelRegistry | None` to `ToolGateway.__init__` with default fallback to `get_kernel_registry()`.
- Implemented `execute_tool(tool_id, args, workspace_dir)` for direct interactive and programmatic invocation.
- Implemented `_tool_code_kernel` handler:
  - Resolves session owner (`session_id`, `_active_session_key`, or `"default"`).
  - Retrieves/spawns kernel via `KernelRegistry.get_or_create`.
  - Executes code cell and redacts secrets on all output streams.
  - Generates stale module warnings if imported project files have changed on disk.
  - Returns structured execution dictionary: `status`, `output`, `stdout`, `stderr`, `execution_count`, `state_reset`, `duration_seconds`, and `warning`.

### 2. Multi-Turn ReAct Loop (`src/harness_core/react_loop.py`)
- Added `session_id` tracking on `ReActLoop` instance, propagating it to gateway tool executions.
- Injected `owner = session_id` into `code_kernel` tool call requests.
- Formatted `code_kernel` observations to cleanly display stdout/stderr alongside any stale module warnings.

### 3. TUI Cockpit (`src/harness_core/tui.py`)
- Added `/kernel` and `/repl` to `HexConsole.SLASH_COMMANDS` for tab-completion.
- Added `/kernel` documentation to `/help`.
- Implemented `_cmd_kernel(arg)` handling:
  - `status`: Displays active kernels, PIDs, execution counts, uptime, and last used times in Rich / ANSI tables.
  - `reset`: Resets active kernel namespace and clears all variables.
  - `vars` / `inspect`: Safely inspects user-defined variables (`k, type, repr`) and displays them in a formatted table.
  - `exec <code>`: Executes code snippet in the active kernel with status and cell count headers.

### 4. Integration Test Suite (`tests/test_gateway_code_kernel.py`)
- 14 comprehensive unit and integration tests:
  - `test_tool_registration_and_risk_class`
  - `test_state_persistence_across_calls`
  - `test_reset_clears_variables`
  - `test_secret_redaction`
  - `test_stale_module_warning`
  - `test_empty_code_returns_error`
  - `test_timeout_handling`
  - `test_risk_policy_gating_r2`
  - `test_react_loop_multi_turn_state_retention`
  - `test_session_id_context_propagation`
  - `test_slash_command_autocompletion_and_help`
  - `test_kernel_status_empty_and_active`
  - `test_kernel_vars_and_reset`
  - `test_kernel_invalid_command_shows_usage`

---

## Verification Evidence

1. **Gateway REPL Verification**:
   ```bash
   .venv/bin/python -c "from harness_core.gateway import ToolGateway; from harness_core.db import LedgerStore; import tempfile, shutil; p = tempfile.mkdtemp(); l = LedgerStore(p + '/test.db'); g = ToolGateway(l); assert 'code_kernel' in g.tools; res = g.execute_tool('code_kernel', {'code': 'x = 100\nprint(x)'}, workspace_dir=p); assert '100' in res['output'], res; res2 = g.execute_tool('code_kernel', {'code': 'print(x + 23)'}, workspace_dir=p); assert '123' in res2['output'], res2; g.kernel_registry.shutdown(); l.close(); shutil.rmtree(p); print('GATEWAY REPL VERIFIED')"
   # Result: GATEWAY REPL VERIFIED
   ```

2. **Integration Test Suite**:
   ```bash
   .venv/bin/python -m unittest tests/test_gateway_code_kernel.py
   # Result: Ran 14 tests in 4.309s — OK
   ```

3. **Updated System Suite Verification**:
   ```bash
   .venv/bin/python -m unittest tests/test_gateway_tools.py tests/test_acp_server.py
   # Result: Ran 23 tests in 37.554s — OK
   ```

4. **Full Test Discovery**:
   - 737 tests discovered across repository, verified green with zero regressions.
