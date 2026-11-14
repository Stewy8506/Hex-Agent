# Phase 27: PTY Background Process Supervisor & Stream Registry — Verification Report

**Phase:** 27 — PTY Background Process Supervisor & Stream Registry (Hermes Parity Pillar 4)  
**Date:** 2026-11-14  
**Auditor:** GSD Verifier  
**Status:** PASS  

---

## 1. Executive Summary

Phase 27 delivers an asynchronous pseudo-terminal (PTY) background process supervisor and circular stream registry in `src/harness_core/process_registry.py`, matching the Hermes Agent background process architecture. The supervisor provides thread-safe lifecycle control (`spawn`, `poll`, `read_output`, `get_tail_output`, `wait`, `kill`, `list_processes`, `cleanup_all`), native POSIX pseudo-terminal allocation with transparent fallback to standard pipes, a rolling 200 KB circular ring buffer (`CircularOutputBuffer`), background non-blocking daemon reader threads, full state transitions (`RUNNING`, `COMPLETED`, `FAILED`, `KILLED`), process group signaling (`os.killpg`) to eliminate orphan/zombie processes, and automated `atexit` teardown hooks.

The supervisor is exposed to agents and human operators through:
1. `ToolGateway` process tools: `start_process` (R2), `read_terminal` (R0), `kill_process` (R2), `list_processes` (R0).
2. Autonomous `ReActLoop` integration for background daemon management.
3. Interactive `HexConsole` TUI slash commands (`/ps`, `/processes`).
4. CLI subcommand (`hex ps`).

All 7 Hermes parity requirements and all phase must-haves have been verified through line-by-line parity analysis and empirical test execution.

---

## 2. Hermes Reference Parity Audit Matrix

| Hermes Reference | Harness Implementation | Parity Status | Evidence & Parity Details |
|---|---|---|---|
| `benchmarks/hermes-agent/tools/process_registry.py` (L61: `MAX_OUTPUT_CHARS = 200_000`, `ProcessSession.output_buffer`) | `src/harness_core/process_registry.py` (`CircularOutputBuffer`, `ProcessSession`) | **FULL PARITY** | 200,000 char default ring buffer; thread-safe locking; head-trimming when exceeded; cumulative `total_chars` counter. |
| `benchmarks/hermes-agent/tools/process_registry.py` & `read_terminal_tool.py` (L35-49: `read_pane(window, offset, count)`) | `src/harness_core/process_registry.py` (`CircularOutputBuffer.read`, `read_output`) & `src/harness_core/gateway.py` (`_tool_read_terminal`) | **FULL PARITY** | Windowed read with `offset` and `limit`; relative buffer offset calculation; `has_more` flag; bounds clamping. |
| `benchmarks/hermes-agent/tools/process_registry_termination.py` (L33-131: SIGTERM parent/descendants, grace window, SIGKILL escalation) | `src/harness_core/process_registry.py` (`ProcessRegistry.kill`, `cleanup_all`) | **FULL PARITY** | Process group targeting via `os.killpg(os.getpgid(pid))`; graceful SIGTERM followed by 1.0s wait; automatic escalation to SIGKILL; instant SIGKILL on `force=True`. |
| `benchmarks/hermes-agent/tools/process_registry.py` (L100-104: PTY spawn + fallback, non-blocking select reader) | `src/harness_core/process_registry.py` (`spawn`, `_reader_loop`) | **FULL PARITY** | POSIX `pty.openpty()` with raw termios variables (`TERM=xterm-256color`, `PAGER=cat`, `GIT_PAGER=cat`), slave fd closed in parent, fallback to `subprocess.PIPE` (`DEVNULL` stdin, `STDOUT` stderr); non-blocking `select.select` reader with incremental UTF-8 decoder and final drain. |
| `benchmarks/hermes-agent/tools/terminal_tool_background.py` (L237: `_redact_terminal_error_text`, secret masking) | `src/harness_core/gateway.py` (`_tool_start_process`, `_tool_read_terminal`, `_tool_list_processes`) | **FULL PARITY** | `redact_secrets` invoked over commands, initial output tail, stream slices, and process list dictionaries. |
| `benchmarks/hermes-agent/tools/process_registry.py` (L562-566: `exited`, `exit_code`, `completion_reason`) | `src/harness_core/process_registry.py` (`ProcessStatus`, `mark_finished`) | **FULL PARITY** | Explicit enum `ProcessStatus`: `RUNNING`, `COMPLETED` (exit code 0), `FAILED` (exit code != 0), `KILLED`. |
| `benchmarks/hermes-agent/tools/process_registry.py` (L646: `mark_exited`, exit code tracking) | `src/harness_core/process_registry.py` (`ProcessRegistry.poll`, `wait`, `_reader_loop`) | **FULL PARITY** | Child exit code harvested via `process.wait()` / `process.poll()`; propagated to session dict, slice queries, and gateway outputs. |

---

## 3. Requirement-by-Requirement Parity Evaluation

### 3.1. Ring Buffer Sizing (200 KB Max Output Chars)
- **Hermes Implementation:** `MAX_OUTPUT_CHARS = 200_000`. In `_append_locked()`, string appends and trims `[-self.max_output_chars:]`.
- **Harness Implementation:** `CircularOutputBuffer(max_chars=200_000)` in `src/harness_core/process_registry.py` (lines 47–121). On `append()`, acquires `threading.Lock`, appends to `_buffer`, increments cumulative `_total_chars`, and executes `self._buffer = self._buffer[-self.max_chars:]`.
- **Audit Finding:** Identical 200,000 char capacity, thread-safe locking, and exact tail slicing behavior.
- **Verdict:** PASS.

### 3.2. Slicing with Offset and Limit
- **Hermes Implementation:** `_output_tail(session, n)` and windowed pane reads with offset / limit.
- **Harness Implementation:** `CircularOutputBuffer.read(offset=0, limit=2000)` in `src/harness_core/process_registry.py` (lines 70–100). Determines buffer boundaries relative to cumulative stream position:
  ```python
  buf_len = len(self._buffer)
  total = self._total_chars
  buffer_start = max(0, total - buf_len)
  if offset < buffer_start:
      start_in_buf = 0
  else:
      start_in_buf = offset - buffer_start
  ```
  Returns `(slice_text, total_chars, has_more)`.
- **Audit Finding:** Accurately maps arbitrary cumulative stream offsets into the rolling buffer window, handling both in-window and truncated-head offsets.
- **Verdict:** PASS.

### 3.3. Process Group Kill / Termination (SIGTERM → SIGKILL Escalation)
- **Hermes Implementation:** `ProcessTerminationMixin._terminate_host_pid()` sends SIGTERM, waits for grace period, and escalates surviving PIDs to SIGKILL.
- **Harness Implementation:** `ProcessRegistry.kill(process_id, force=False)` in `src/harness_core/process_registry.py` (lines 469–529):
  1. Identifies process group: `pgid = os.getpgid(pid)`.
  2. Dispatches initial signal via `os.killpg(pgid, signal.SIGTERM)` (or `os.kill` fallback).
  3. If `not force`, enters grace timeout `session.process.wait(timeout=1.0)`.
  4. If process survives the grace window, escalates to `os.killpg(pgid, signal.SIGKILL)`.
  5. If `force=True`, immediately issues `signal.SIGKILL`.
  6. Reaps exit code (-9), sets `_stop_event`, closes open PTY master fd, and marks status `ProcessStatus.KILLED`.
  7. Registers `atexit.register(self.cleanup_all)` to guarantee no orphaned background processes survive session exit.
- **Audit Finding:** Conforms to Hermes termination and escalation model; prevents orphan zombie trees via process group signaling.
- **Verdict:** PASS.

### 3.4. Non-Blocking Async Stdout/Stderr Streaming via PTY with Pipe Fallback
- **Hermes Implementation:** PTY allocation with fallback to subprocess pipes; non-blocking select loop reading chunks into the buffer.
- **Harness Implementation:** `ProcessRegistry.spawn()` and `_reader_loop()` in `src/harness_core/process_registry.py` (lines 180–398):
  1. Attempts POSIX `pty.openpty()`. Sets terminal variables (`TERM=xterm-256color`, `PAGER=cat`, `GIT_PAGER=cat`).
  2. Spawns subprocess with `preexec_fn=os.setsid` and `close_fds=True`.
  3. Parent immediately closes slave descriptor to ensure clean EOF delivery.
  4. If PTY allocation or launch fails, seamlessly falls back to `subprocess.PIPE` (`DEVNULL` stdin, `STDOUT` stderr, `start_new_session=True`).
  5. Background daemon thread `proc-reader-<id>` performs non-blocking `select.select([fd], [], [], 0.1)` reads with 4096-byte chunks.
  6. Employs `codecs.getincrementaldecoder("utf-8")(errors="replace")` for multi-byte UTF-8 boundary preservation.
  7. On process exit, executes up to 50 drain iterations with `select.select([fd], [], [], 0.02)` and a final flush.
- **Audit Finding:** Full async non-blocking PTY streaming with robust pipe fallback and stream drain.
- **Verdict:** PASS.

### 3.5. Secret Scrubbing in Command Execution and Output Reads
- **Hermes Implementation:** Masking and redacting sensitive tokens in logs and process results.
- **Harness Implementation:** `src/harness_core/gateway.py` (lines 1947–2023):
  - `_tool_start_process`: Scrubbed initial output tail via `redact_secrets()`; entire returned payload scrubbed before returning.
  - `_tool_read_terminal`: Output slice passed through `redact_secrets()`.
  - `_tool_list_processes`: Process records passed through `redact_secrets()`.
  - Verified by `test_secret_scrubbing_in_process_tools`: tokens matching `sk-ant-***` are redacted into `sk-ant-***[REDACTED]***`.
- **Audit Finding:** Consistent secret scrubbing on every process tool boundary.
- **Verdict:** PASS.

### 3.6. Status Transitions (RUNNING, COMPLETED, FAILED, KILLED)
- **Hermes Implementation:** Process lifecycle states tracking live, finished, failed, and killed processes.
- **Harness Implementation:** `ProcessStatus` enum in `src/harness_core/process_registry.py`:
  - `RUNNING = "running"`: Initial state on spawn.
  - `COMPLETED = "completed"`: Reaped exit code == 0.
  - `FAILED = "failed"`: Reaped exit code != 0.
  - `KILLED = "killed"`: Terminated via `kill()`.
- **Audit Finding:** Explicit transitions matching all lifecycle states.
- **Verdict:** PASS.

### 3.7. Exit Code Capture and Error Reporting
- **Hermes Implementation:** Harvests subprocess exit status and includes in session records.
- **Harness Implementation:**
  - Reader thread and `poll()` harvest exit code via `process.wait()` / `process.poll()`.
  - Session dictionary (`to_dict()`) and stream read responses (`read_output()`) report `exit_code`.
  - Gated tools in `gateway.py` return error payloads with `"ok": False` and descriptive messages when commands fail, parameters are missing, or process IDs do not exist.
- **Audit Finding:** Full exit code capture and structured error reporting.
- **Verdict:** PASS.

---

## 4. Operator & Tool Interface Verification

### 4.1. ToolGateway Process Management Tools
In `src/harness_core/gateway.py`:
- `start_process`: Registered with `RiskClass.R2`, parameters `command` (required), `cwd`, `pty`. Confines working directory to workspace boundary using `_resolve_confined_path`.
- `read_terminal`: Registered with `RiskClass.R0`, parameters `process_id` (required), `offset`, `limit`.
- `kill_process`: Registered with `RiskClass.R2`, parameters `process_id` (required), `force`.
- `list_processes`: Registered with `RiskClass.R0`, parameters `status` (`running`, `completed`, `failed`, `killed`, `all`).

### 4.2. TUI Cockpit (`/ps` & `/processes`)
In `src/harness_core/tui.py`:
- Registered in `handle_slash_command`, `SLASH_COMMANDS` autocompletion list, and `_cmd_help()`.
- `/ps`: Renders table of tracked processes showing `ID`, `PID`, `STATUS` (colorized badges), `UPTIME`, `PTY`, `COMMAND`, and `OUTPUT TAIL`. Utilizes Rich `Table` if available with ANSI text fallback.
- `/ps <proc_id>`: Displays comprehensive details and 2000-character terminal tail.
- `/ps kill <proc_id>`: Terminates the target process and prints status confirmation.

### 4.3. CLI Subcommand (`hex ps`)
In `src/harness_core/cli.py`:
- `hex ps`: Formatted table listing of active and completed processes.
- `hex ps --json`: Machine-readable JSON output.
- `hex ps --kill <id> [--force]`: Signals process termination via SIGTERM or SIGKILL.
- `hex ps --filter <status>`: Filters by status.

---

## 5. Empirical Verification Evidence

### 5.1. Unit Tests (`tests/test_process_registry.py`)
Command:
```bash
.venv/bin/python -m unittest tests/test_process_registry.py
```
Output:
```
.............
----------------------------------------------------------------------
Ran 13 tests in 0.367s

OK
```

Tests verified:
1. `test_buffer_rollover_max_chars`: Ring buffer capping at `max_chars` with cumulative character tracking.
2. `test_cleanup_all`: Teardown of all active processes and reader threads.
3. `test_empty_command_raises`: Validation preventing empty commands.
4. `test_get_nonexistent_process_raises`: KeyError on invalid process IDs.
5. `test_get_tail_output_and_ansi_strip`: Tail retrieval with ANSI escape code stripping.
6. `test_kill_graceful_and_force`: Process group termination with SIGTERM and SIGKILL.
7. `test_list_processes_and_filtering`: Filtering by status enum.
8. `test_pipe_mode_explicit`: Clean fallback to standard pipes.
9. `test_read_output_slicing_and_pagination`: Offset and limit slicing from rolling buffer.
10. `test_singleton_registry`: Singleton lifecycle management.
11. `test_spawn_and_poll_completed_process`: Fast command lifecycle and exit code 0.
12. `test_spawn_failing_command`: Non-zero exit code capture and status `failed`.
13. `test_streaming_background_process`: Incremental multi-chunk stream consumption.

### 5.2. Gateway & End-to-End Integration Tests (`tests/test_gateway_process_tools.py`)
Command:
```bash
.venv/bin/python -m unittest tests/test_gateway_process_tools.py
```
Output:
```
...........
----------------------------------------------------------------------
Ran 11 tests in 7.332s

OK
```

Tests verified:
1. `test_cli_hex_ps_subcommand`: CLI invocation of `hex ps` (listing, json, kill, filtering).
2. `test_fast_path_gating`: Policy enforcement permitting R0 reads while denying R2 spawns.
3. `test_kill_process_tool`: Signal delivery and state transition to `killed`.
4. `test_legacy_process_tool_compatibility`: Backward compatibility for `process(action='list'|'kill')`.
5. `test_list_processes_tool`: Supervised process enumeration.
6. `test_path_confinement_on_spawn`: Security confinement preventing traversal outside workspace.
7. `test_react_loop_process_lifecycle`: End-to-end multi-turn ReAct autonomous process control.
8. `test_read_terminal_paging_and_offset`: Incremental paging across ring buffer.
9. `test_secret_scrubbing_in_process_tools`: Token redaction on command strings and stream outputs.
10. `test_start_process_tool_execution`: Process spawning and initial output capture.
11. `test_tui_ps_slash_command`: TUI `/ps` table and detail rendering.

### 5.3. Combined Verification Run
Command:
```bash
.venv/bin/python -m unittest tests/test_process_registry.py tests/test_gateway_process_tools.py tests/test_gateway_tools.py
```
Output:
```
......................................
----------------------------------------------------------------------
Ran 38 tests in 10.156s

OK
```

### 5.4. CLI Smoke Test
Command:
```bash
.venv/bin/hex ps
```
Output:
```
No background processes found.
```

Command:
```bash
.venv/bin/hex ps --json
```
Output:
```
[]
```

---

## 6. Verdict

**Verdict:** PASS  
**Phase:** 27 — PTY Background Process Supervisor & Stream Registry  
**Parity Score:** 7/7 Hermes Parity Pillars Verified  
**Must-Haves Met:** 5/5  
**Regressions:** 0  

Phase 27 has satisfied all requirements specified in `.gsd/ROADMAP.md` and `.gsd/SPEC.md`. The PTY background process supervisor is complete, fully tested, and ready for production use.
