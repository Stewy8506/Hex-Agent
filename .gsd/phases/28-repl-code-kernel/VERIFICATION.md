# Phase 28: Persistent REPL Code Kernel with Cross-Turn State — Verification Report

**Phase Goal:** Implement an interactive persistent Python code kernel in `src/harness_core/code_kernel.py` matching `tools/code_kernel.py` in Hermes Agent, enabling persistent variables, functions, and imports across consecutive turns within the task CoW workspace.

**Verification Status:** PASS  
**Auditor:** GSD Verifier  
**Reference Codebase:** `benchmarks/hermes-agent/tools/code_kernel.py` & `benchmarks/hermes-agent/tests/tools/test_code_kernel.py`  
**Target Codebase:**
- `src/harness_core/code_kernel.py`
- `src/harness_core/gateway.py`
- `src/harness_core/react_loop.py`
- `src/harness_core/tui.py`
- `tests/test_code_kernel.py`
- `tests/test_gateway_code_kernel.py`

---

## 1. Hermes Parity Line-by-Line Audit Matrix

| Requirement Area | Hermes Reference (`benchmarks/hermes-agent/tools/code_kernel.py`) | Harness Core (`src/harness_core/code_kernel.py`, `gateway.py`, `react_loop.py`, `tui.py`) | Parity Status | Evidence & Details |
|---|---|---|---|---|
| **1. Wire Protocol & Framing** | Lines 128, 206–210 (`_reply`), Lines 521–575 (`_stdout_reader`), Lines 577–582 (`_stderr_reader`): Sentinel string framing `\n<SENTINEL> <length>\n<body>` over `sys.stdout.buffer`, chunked read1, length parsing, JSON decode, and raw byte pass-through. | `src/harness_core/code_kernel.py` Lines 126, 150–155 (`_reply`), Lines 280–337 (`_stdout_reader`), Lines 339–351 (`_stderr_reader`): Identical sentinel framing `\n<SENTINEL> <length>\n<body>`, length extraction, non-blocking chunk reading, JSON decoding, raw byte fallback into `raw_buffer`, and distinct `stderr_buffer`. | **PASS** | `test_stdout_and_stderr_separation` verified streams arrive in separate fields without leakage. |
| **2. Namespace & State Retention** | Lines 48–49 (`RUNNER_CELL_SOURCE`): `GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}`, cell exec in `GLOBALS`. | `src/harness_core/code_kernel.py` Lines 44–45, 101: `GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}`, persistent across cells. | **PASS** | `test_cross_call_variable_persistence` & `test_state_persistence_across_calls` verified variable/function persistence across sequential turns. |
| **3. Module Staleness Detection** | Lines 55–90: `_MODULE_RECORDS`, `_scan_edited_modules(report)` tracking mtime and `__spec__` of imported project files, ignoring stdlib & site-packages. Lines 794–809: `stale_modules_warning` format string advising `importlib.reload(...)` or `reset=True`. | `src/harness_core/code_kernel.py` Lines 52–85: `_scan_edited_modules(report)` safely using `object.__getattribute__` to avoid lazy loaders, comparing `mtime_ns` and `spec`. Lines 200–218: `stale_modules_warning` advisory with secret redaction. | **PASS** | `test_stale_module_detection` & `test_stale_module_warning` verified on-disk source file modifications trigger stale module advisories. |
| **4. Output Capping & Disk Spill** | Line 43: `_RUNNER_CAPTURE_BYTES = 1_000_000` (1MB), Lines 130–131, 191–204: 5MB disk spill (`_SPILL_CAP = 5_000_000`) writing full stdout to `cell_XXXXXX_stdout.txt` in spill directory when clipped. | `src/harness_core/code_kernel.py` Lines 37–38, 127–129, 135–147, 530–536: 1MB in-memory capture cap, 5MB disk spill cap to `HEX_KERNEL_SPILL_DIR`, with paging advisory warning returned to agent. | **PASS** | `test_output_capping_and_spill` verified 1.2MB stdout is clipped to 1MB and full output saved to disk. |
| **5. Timeout & Wedged Process Termination** | Lines 351–352, 783–784, 824–826: `_kill_process_group` (SIGTERM escalated to SIGKILL), `_REGISTRY.discard(key, kernel)` dropping dead kernel immediately from registry, error reported cleanly. | `src/harness_core/code_kernel.py` Lines 245–278 (`_kill_process_tree` with `os.killpg(pgid, sig)`), Lines 495–512 (escalates to SIGKILL on timeout), `discard_kernel` dropping dead kernel from registry and tracking `_reset_keys`. `gateway.py` Line 2309: drops timed out kernel. | **PASS** | Standalone test & `test_timeout_handling` verified `len(registry._kernels) == 0` immediately upon timeout, and subsequent call recovers with `state_reset: True`. |
| **6. Reset & Lifecycle Management** | Lines 369–394 (`KernelRegistry`), Lines 403–404 (`DEFAULT_MAX_SESSION_KERNELS=4`, `DEFAULT_KERNEL_IDLE_TIMEOUT=1800`), Lines 684–711 (`_acquire_kernel`), Lines 745–775 (background idle reaper), Line 499 (`atexit.register`). | `src/harness_core/code_kernel.py` Lines 585–702 (`KernelRegistry` with `max_kernels=4`, `idle_timeout=1800.0`, LRU eviction, background reaper thread), Lines 726–731 (`atexit.register`). | **PASS** | `test_kernel_reset_clears_state`, `test_registry_isolation_and_lru_eviction`, and `test_registry_idle_sweep` all pass. |
| **7. Security, Policy & Secret Scrubbing** | Hermes `redact_sensitive_text(code_file=True)` on outputs, tracebacks, and warnings; process execution inside sandboxed target cwd. | `src/harness_core/gateway.py` Lines 580–594, 945–952, 2310–2323: Default-deny `RiskClass.R2`, rejected on fast-path tasks; `redact_secrets` applied to stdout, stderr, traceback, and warning strings. Subprocess spawned in `workspace_dir`. | **PASS** | `test_secret_redaction` & `test_risk_policy_gating_r2` verified token scrubbing and R2 policy enforcement. |

---

## 2. Component Integration Verification

### 2.1 ToolGateway (`src/harness_core/gateway.py`)
- **Schema & Registration:** Tool `code_kernel` registered in `TOOL_DEFINITIONS` with `risk_class: RiskClass.R2`, parameters `code` (required), `reset` (boolean, default False), and `timeout` (number, default 30.0s).
- **Execution:** Programmatic dispatch via `execute_tool("code_kernel", args, workspace_dir)`.
- **Policy Enforcement:** Fully evaluated through `evaluate_policy`:
  - Standard tasks (`auto`): ALLOW (R2 permitted).
  - Fast-Path tasks (`fast_path`): DENY (`PolicyBlockedError`, only R0/R1 allowed).
  - Constrained tasks: DENY if `code_kernel` not in `allowed_tools`.
- **Secret Redaction:** `redact_secrets` scrubs API tokens (`ghp_...`, `sk-...`, etc.) from stdout, stderr, and tracebacks.

### 2.2 Multi-Turn ReActLoop (`src/harness_core/react_loop.py`)
- **Session Identity:** `self.session_id` configured and passed through reasoning cycles.
- **Context Injection:** Injects `call.arguments["owner"] = self.session_id` into `code_kernel` tool calls and assigns `self.gateway._active_session_key = self.session_id`.
- **Multi-Turn State Retention:** Variables defined in turn 1 (`numbers = [10, 20, 30]`) survive into turn 2 (`sum(numbers)` -> `60`).
- **Observation Formatting:** Formats kernel results cleanly into message history with `[Warning: ...]` when modules are stale.

### 2.3 HexConsole TUI (`src/harness_core/tui.py`)
- **Command Routing:** Tab-autocompleted slash commands `/kernel` and `/repl` registered in `SLASH_COMMANDS` and documented in `/help`.
- **Subcommands:**
  - `/kernel status`: Renders Rich / ANSI table of active kernels showing `OWNER`, `CWD`, `STATUS` (RUNNING/DEAD/PENDING), `PID`, `EXEC COUNT`, `UPTIME`, and `LAST USED`.
  - `/kernel reset`: Wipes kernel namespace, prints confirmation.
  - `/kernel vars`: Runs non-destructive JSON inspection snippet across `GLOBALS`, rendering variable names, types, and values in formatted tables.
  - `/kernel exec <code>`: Executes code snippet interactively from the TUI prompt with execution count and status headers.

---

## 3. Empirical Test Suite Evidence

### 3.1 Kernel Engine Unit Tests (`tests/test_code_kernel.py`)
```bash
.venv/bin/python -m unittest tests/test_code_kernel.py
```
**Output:**
```
............
----------------------------------------------------------------------
Ran 12 tests in 1.134s

OK
```
Tests verified:
- `TestBoundedBuffer.test_bounded_buffer_cap_and_drain`
- `TestCodeKernel.test_cross_call_variable_persistence`
- `TestCodeKernel.test_stdout_and_stderr_separation`
- `TestCodeKernel.test_output_capping_and_spill`
- `TestCodeKernel.test_stale_module_detection`
- `TestCodeKernel.test_execution_timeout_kills_process`
- `TestCodeKernel.test_kernel_reset_clears_state`
- `TestCodeKernel.test_syntax_and_runtime_exception_formatting`
- `TestCodeKernel.test_system_exit_handling`
- `TestKernelRegistry.test_registry_isolation_and_lru_eviction`
- `TestKernelRegistry.test_registry_idle_sweep`
- `TestKernelRegistry.test_stale_modules_warning_helper`

### 3.2 Gateway, ReActLoop & TUI Integration Tests (`tests/test_gateway_code_kernel.py`)
```bash
.venv/bin/python -m unittest tests/test_gateway_code_kernel.py
```
**Output:**
```
..............
----------------------------------------------------------------------
Ran 14 tests in 4.477s

OK
```
Tests verified:
- `TestToolGatewayCodeKernel.test_tool_registration_and_risk_class`
- `TestToolGatewayCodeKernel.test_state_persistence_across_calls`
- `TestToolGatewayCodeKernel.test_reset_clears_variables`
- `TestToolGatewayCodeKernel.test_secret_redaction`
- `TestToolGatewayCodeKernel.test_stale_module_warning`
- `TestToolGatewayCodeKernel.test_empty_code_returns_error`
- `TestToolGatewayCodeKernel.test_timeout_handling` (including immediate registry drop & recovery)
- `TestToolGatewayCodeKernel.test_risk_policy_gating_r2`
- `TestReActLoopCodeKernelIntegration.test_react_loop_multi_turn_state_retention`
- `TestReActLoopCodeKernelIntegration.test_session_id_context_propagation`
- `TestHexConsoleKernelSlashCommand.test_slash_command_autocompletion_and_help`
- `TestHexConsoleKernelSlashCommand.test_kernel_status_empty_and_active`
- `TestHexConsoleKernelSlashCommand.test_kernel_vars_and_reset`
- `TestHexConsoleKernelSlashCommand.test_kernel_invalid_command_shows_usage`

### 3.3 Gateway Regression Verification (`tests/test_gateway_tools.py`, `tests/test_acp_server.py`)
```bash
.venv/bin/python -m unittest tests/test_gateway_tools.py tests/test_acp_server.py
```
**Output:**
```
Ran 23 tests in 37.467s

OK
```

### 3.4 Immediate Registry Drop & State Recovery Verification
```bash
.venv/bin/python -c "
from harness_core.code_kernel import KernelRegistry
from harness_core.gateway import ToolGateway
from harness_core.db import LedgerStore
import tempfile, shutil

tmp = tempfile.mkdtemp()
ledger = LedgerStore(tmp + '/test.db')
reg = KernelRegistry()
gw = ToolGateway(ledger, kernel_registry=reg)

# 1. timeout
res1 = gw.execute_tool('code_kernel', {'code': 'import time; time.sleep(1.0)', 'timeout': 0.2}, workspace_dir=tmp)
print('res1 status:', res1['status'])
print('Kernels in registry immediately after timeout:', len(reg._kernels))

# 2. subsequent call
res2 = gw.execute_tool('code_kernel', {'code': 'print(12345)'}, workspace_dir=tmp)
print('res2 status:', res2['status'], 'output:', res2['output'].strip(), 'state_reset:', res2['state_reset'])

reg.shutdown()
ledger.close()
shutil.rmtree(tmp)
print('TIMEOUT AND RECOVERY VERIFICATION SUCCESS')
"
```
**Output:**
```
res1 status: timeout
Kernels in registry immediately after timeout: 0
res2 status: success output: 12345 state_reset: True
TIMEOUT AND RECOVERY VERIFICATION SUCCESS
```

---

## 4. Final Verdict

All 7 Hermes parity requirements, all 7 truths of Plan 28-01, and all 5 truths of Plan 28-02 have been empirically verified with 100% pass rate. Phase 28 is fully verified and ready to be marked complete.
