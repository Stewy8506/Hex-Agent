# Plan 27.01: PTY ProcessRegistry & Circular Ring Buffer Core Summary

**Phase:** 27 — PTY Background Process Supervisor & Stream Registry  
**Plan:** 01 — PTY ProcessRegistry & Circular Ring Buffer Core  
**Wave:** 1  
**Status:** Complete  

---

## 1. Objective Accomplished

Implemented a thread-safe asynchronous pseudo-terminal (PTY) process supervisor and rolling circular stream buffer in `src/harness_core/process_registry.py` matching the Hermes Agent background process architecture. This provides process lifecycle management (`spawn`, `poll`, `read_output`, `get_tail_output`, `wait`, `kill`, `list_processes`, `cleanup_all`), native POSIX pseudo-terminal allocation with automatic transparent pipe fallback, a rolling 200 KB circular ring buffer (`CircularOutputBuffer`), background non-blocking daemon reader threads, process states (`RUNNING`, `COMPLETED`, `FAILED`, `KILLED`), process group signaling (`os.killpg`) to eliminate orphan/zombie processes, and `atexit` teardown hooks.

---

## 2. Key Deliverables & Changes

1. **CircularOutputBuffer (`src/harness_core/process_registry.py`):**
   - Implemented thread-safe circular ring buffer with default `max_chars = 200_000` (200 KB) matching Hermes `MAX_OUTPUT_CHARS`.
   - Cumulative total character tracking (`total_chars`) with head-trimming upon capacity breach.
   - Offset and limit slice reader (`read(offset, limit) -> tuple[str, int, bool]`) supporting relative positioning from cumulative stream start or current ring buffer window.
   - `get_tail(n, strip_ansi=True)` returning terminal tail with optional regex-based ANSI escape sequence removal.

2. **ProcessSession & ProcessStatus (`src/harness_core/process_registry.py`):**
   - `ProcessStatus` string enum: `RUNNING`, `COMPLETED`, `FAILED`, `KILLED`.
   - `ProcessSession` dataclass tracking process metadata (`id`, `command`, `cwd`, `pid`, `status`, `started_at`, `finished_at`, `exit_code`, `pty_enabled`, `buffer`, `pty_master_fd`, `process`).
   - Clean serializable dictionary projection (`to_dict()`) including uptime calculations.
   - State transition and exit marking via `mark_finished()`.

3. **ProcessRegistry Supervisor (`src/harness_core/process_registry.py`):**
   - POSIX PTY allocation using `pty.openpty()`, closing slave descriptor in parent, and configuring default interactive terminal variables (`TERM=xterm-256color`, `PAGER=cat`, `GIT_PAGER=cat`).
   - Seamless fallback to `subprocess.PIPE` if PTY is unsupported or unavailable.
   - Background daemon thread (`_reader_loop`) using non-blocking `select.select` and incremental UTF-8 decoding (`codecs.getincrementaldecoder`).
   - Supervised operations:
     - `spawn(command, cwd, env, use_pty=True)`
     - `poll(process_id)`
     - `read_output(process_id, offset, limit)`
     - `wait(process_id, timeout)`
     - `kill(process_id, force=False)` with POSIX process group termination via `os.killpg(os.getpgid(pid))`
     - `list_processes(status_filter=None)`
     - `cleanup_all()` terminating all active processes and closing file descriptors
     - `get_process_registry()` singleton and `reset_process_registry()` for test isolation
     - `atexit.register(self.cleanup_all)` ensuring zero orphaned background processes on session exit.

4. **Comprehensive Test Suite (`tests/test_process_registry.py`):**
   - 13 unit and integration tests covering:
     - Buffer rollover and cumulative character tracking at 200 KB threshold
     - Slice reads and EOF boundaries
     - Tail reads with ANSI stripping
     - Fast-exiting commands under PTY and pipe modes (exit code 0, status `completed`)
     - Failing commands (exit code 42, status `failed`)
     - Incremental background streaming commands
     - Graceful (`SIGTERM`) and forced (`SIGKILL`) process group termination
     - Process listing and status filtering
     - Automated `cleanup_all` and singleton management
     - Parameter validation and error conditions.

---

## 3. Verification & Empirical Proof

- **Task 1 Verification:**
  ```bash
  .venv/bin/python -c "from harness_core.process_registry import CircularOutputBuffer, ProcessStatus, ProcessSession; buf = CircularOutputBuffer(max_chars=100); buf.append('a'*60); buf.append('b'*60); assert len(buf.read(0, 200)[0]) == 100; assert buf.total_chars == 120; print('CircularOutputBuffer OK')"
  # Output: CircularOutputBuffer OK
  ```

- **Task 2 Verification:**
  ```bash
  .venv/bin/python -m unittest tests/test_process_registry.py
  # Output: Ran 13 tests in 0.360s -- OK
  ```

- **Full Repository Regression Suite:**
  ```bash
  .venv/bin/python -m unittest discover -s tests
  # Output: Ran 700 tests in 559.264s -- OK (skipped=1)
  ```

---

## 4. Commits

- `157ef94`: feat(phase-27): circular output buffer and process session state engine
- `a82bc11`: feat(phase-27): process registry lifecycle, pty allocation, and reader threads
