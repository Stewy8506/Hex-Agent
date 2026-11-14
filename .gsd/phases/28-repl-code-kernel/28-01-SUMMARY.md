# Plan 28-01: Persistent In-Memory Python CodeKernel & KernelRegistry Summary

## 1. Overview
Delivered the persistent REPL code kernel engine and bounded kernel registry in `src/harness_core/code_kernel.py` matching the Hermes Agent reference (`benchmarks/hermes-agent/tools/code_kernel.py`). This establishes an isolated child Python subprocess supervisor for executing multi-turn code cells while preserving namespace state (`GLOBALS = {"__name__": "__main__", "__builtins__": __builtins__}`), communicating via a cryptographic sentinel framed protocol, enforcing output clipping with disk spill fallback, tracking stale imported modules edited on disk, terminating wedged executions via SIGKILL, and managing process lifecycles via a thread-safe `KernelRegistry`.

## 2. Key Accomplishments

### Task 1: Runner Script, Framed Protocol & Persistent CodeKernel Subprocess Engine (`src/harness_core/code_kernel.py`)
- **Child Runner Generation:** Authored `KERNEL_RUNNER_SOURCE` and `RUNNER_CELL_SOURCE` running a continuous request loop over `sys.stdin` with `contextlib.redirect_stdout` and `contextlib.redirect_stderr`.
- **Sentinel Framed Wire Protocol:** Implemented bidirectional framing using dynamic cryptographic sentinels (`@@HEX-KERNEL-<token>@@`):
  `b"\n" + sentinel.encode() + b" " + str(len(body)).encode() + b"\n" + body`
  Separating protocol JSON responses from uncaptured subprocess raw stream bytes.
- **Bounded Buffer & Output Capping:** Implemented `_BoundedBuffer` capping memory accumulation at 1 MB (`_RUNNER_CAPTURE_BYTES = 1_000_000`). When outputs exceed 1 MB, output is clipped and full output (up to 5 MB) is spilled to disk in `cell_XXXXXX_stdout.txt`.
- **Stale Module Tracking:** Implemented `_scan_edited_modules` tracking imported project module source paths, `st_mtime_ns`, and `__spec__`. When source files are edited on disk after import, the runner identifies them and emits warnings suggesting `importlib.reload(...)` or `reset=True`.
- **Process Group & Timeout Termination:** Configured `start_new_session=True` on POSIX and `_kill_process_tree` with escalation from SIGTERM to SIGKILL (`os.killpg`), preventing orphaned or wedged processes on execution timeouts.
- **Commit:** `29d44d5 feat(phase-28): persistent repl code kernel subprocess engine and kernel registry`

### Task 2: KernelRegistry, LRU / Idle Eviction, Stale Warning Formatter & Unit Test Suite (`src/harness_core/code_kernel.py`, `tests/test_code_kernel.py`)
- **KernelRegistry Lifecycle & LRU Pooling:** Built thread-safe registry keyed by `(owner, cwd)` with bounded capacity (`max_kernels=4`), idle timeout sweeping (`idle_timeout=1800`), unpinned LRU eviction, and graceful shutdown.
- **Background Idle Reaper:** Periodic background daemon thread sweeping expired idle kernels without blocking interactive execution.
- **Stale Module Warning Formatter:** Implemented `stale_modules_warning(stale_modules)` producing model-friendly advisories with secret redaction.
- **Comprehensive Test Suite:** Authored 12 unit and integration tests in `tests/test_code_kernel.py` covering:
  - Cross-call variable and function persistence.
  - Independent stdout and stderr stream separation.
  - 1 MB output clipping and on-disk file spilling.
  - Stale module modification detection on disk.
  - Execution timeout kill escalation via SIGKILL and subsequent recovery.
  - Clean namespace reset via `reset()`.
  - Multi-owner isolation and LRU capacity eviction.
  - SyntaxError and RuntimeError traceback reporting without process termination.
  - Clean SystemExit handling.
- **Full Verification:** All 12/12 tests in `tests/test_code_kernel.py` passed in 1.25s. Verified zero regressions across the entire repository test suite (723/723 tests passed in 599s).
- **Commit:** `1012d8e feat(phase-28): kernel registry lru eviction stale warning formatter and unit test suite`

## 3. Deviations from Plan
None. All implementations and unit tests strictly adhered to specification and the Hermes reference implementation.

## 4. Verification Evidence
- Task 1 `<verify>` command:
  `.venv/bin/python -c "from harness_core.code_kernel import CodeKernel; k = CodeKernel(); res = k.execute('x = 42\nprint(x)'); assert '42' in res['stdout'], res; res2 = k.execute('print(x + 8)'); assert '50' in res2['stdout'], res2; k.close(); print('VERIFICATION SUCCESS')"`
  Result: `VERIFICATION SUCCESS`
- Task 2 `<verify>` command:
  `.venv/bin/python -m unittest tests/test_code_kernel.py`
  Result: `Ran 12 tests in 1.227s - OK`
- Full test discovery:
  `.venv/bin/python -m unittest discover -s tests`
  Result: `Ran 723 tests in 599.333s - OK (skipped=1)`
