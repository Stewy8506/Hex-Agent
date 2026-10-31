# Phase 02 Plan 02: Container Sandbox Isolation Engine & Gateway Integration Summary

**Plan:** `02-02-PLAN.md`  
**Phase:** `02-overlayfs-sandbox`  
**Status:** Complete  
**Commits:** `c063f5c`, `bf077fc`, `5cbeaed`  
**Tests Passing:** 76/76 (18 new sandbox isolation tests + 58 existing harness tests)

---

## 1. Executive Summary

Plan 02-02 delivers the container and process isolation execution engine for untrusted agent code evaluation and tool invocations. It fulfills the security guarantees of PRD Sections 1 (Execution & Isolation Layer) and 3 (Durability & Security proofs) by implementing:
1. **Container & Process Sandboxes (`src/harness_core/sandbox.py`):**
   - `BubblewrapSandbox`: Linux unprivileged user namespace container (`bwrap`) with `--unshare-net`, `--unshare-pid`, `--unshare-ipc`, read-only system roots, tmpfs scratch, and workspace bind mounting.
   - `DockerPodmanSandbox`: OCI container containment with `--network none`, `--read-only` root, tmpfs mounts, and workspace directory mapping.
   - `RestrictedSubprocessSandbox`: Safe POSIX/macOS fallback that strictly validates canonical directory containment, purges sensitive environment credentials matching `(?i)(API_KEY|SECRET|TOKEN|PASS|PASSWORD|AUTH|CREDENTIAL|PRIVATE|AWS_|GITHUB_|GH_|HARNESS_)`, manages process groups (`os.setsid`/`os.killpg`), and enforces POSIX resource limits (`RLIMIT_CPU`, `RLIMIT_NPROC`).
   - `get_default_sandbox`: Auto-detection factory selecting bubblewrap on Linux, docker/podman if running, or falling back to restricted subprocess.
2. **Tool Gateway Sandbox Containment (`src/harness_core/gateway.py`):**
   - Enhanced `_resolve_confined_path` with strict `Path.relative_to` containment check, preventing symlink escapes or string prefix collisions.
   - Delegated `_tool_run_tests` to `self.sandbox.execute(...)`, returning structured `SandboxResult` metadata (`timed_out`, `violation_detected`, `sandbox_backend`), raising `PolicyBlockedError` on security violations, and running secret redaction over combined outputs.
3. **Orchestrator Sandbox Integration (`src/harness_core/orchestrator.py`):**
   - Injected `ExecutionSandbox` into `HarnessOrchestrator`, passing it to `ToolGateway` and registering active sandbox capabilities (`sandbox_backend`) on local worker registration.
4. **Zero-Escape Verification Test Suite (`tests/test_sandbox_isolation.py`):**
   - 18 empirical tests verifying workspace containment, path traversal blocking, parent/passed environment purging, timeout process termination, resource exhaustion handling, bwrap/docker command assembly, tool gateway secret redaction, and auto-detection fallbacks.

---

## 2. Completed Tasks

| Task | Description | Status | Commit |
|------|-------------|--------|--------|
| Task 1 | Implement Container Sandbox Isolation Engine in `src/harness_core/sandbox.py` | Complete | `c063f5c` |
| Task 2 | Integrate `ExecutionSandbox` into `ToolGateway` and `Orchestrator` | Complete | `bf077fc` |
| Task 3 | Author zero-escape and sandbox isolation verification test suite in `tests/test_sandbox_isolation.py` | Complete | `5cbeaed` |

---

## 3. Verification Evidence

### Task 1: Sandbox Detection & Factory Instantiation
```bash
$ .venv/bin/python -c "from harness_core.sandbox import get_default_sandbox, RestrictedSubprocessSandbox, BubblewrapSandbox, DockerPodmanSandbox; s = get_default_sandbox(); print('Sandbox detected:', s.backend_name)"
Sandbox detected: restricted_subprocess
```

### Task 2: Gateway & Orchestrator Wiring
```bash
$ .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; from harness_core.gateway import ToolGateway; print('Orchestrator and Gateway successfully wired with sandbox')"
Orchestrator and Gateway successfully wired with sandbox
```

### Task 3: Sandbox Isolation Verification Suite (18 tests)
```bash
$ .venv/bin/python -m unittest tests/test_sandbox_isolation.py -v
test_bubblewrap_mock_execution_returns_sandbox_result ... ok
test_build_bwrap_args_includes_isolation_flags ... ok
test_build_container_args_includes_isolation_flags ... ok
test_docker_mock_execution_returns_sandbox_result ... ok
test_environment_variable_purging_strips_sensitive_credentials ... ok
test_resource_limit_handling_terminates_cleanly ... ok
test_timeout_enforcement_terminates_runaway_process ... ok
test_workspace_boundary_enforcement_blocks_nonexistent_directory ... ok
test_workspace_boundary_enforcement_blocks_outside_directory ... ok
test_workspace_boundary_enforcement_blocks_traversal_path ... ok
test_workspace_valid_inside_execution ... ok
test_docker_podman_detected_when_bwrap_unavailable ... ok
test_fallback_to_restricted_subprocess ... ok
test_linux_bubblewrap_detected_when_available ... ok
test_destructive_file_action_path_escape_raises_policy_blocked_error ... ok
test_tool_run_tests_executes_in_sandbox_and_reports_backend ... ok
test_tool_run_tests_redacts_secrets_in_output ... ok
test_tool_run_tests_violation_raises_policy_blocked_error ... ok

----------------------------------------------------------------------
Ran 18 tests in 2.168s

OK
```

### Full Repository Regression Verification (76 tests)
```bash
$ .venv/bin/python -m unittest discover -s tests -p "test_*.py"
----------------------------------------------------------------------
Ran 76 tests in 2.918s

OK
```

---

## 4. Key Decisions & Rationale

1. **Defense-in-Depth Path Containment:**
   - In both `RestrictedSubprocessSandbox` and `ToolGateway._resolve_confined_path`, path containment is validated with `Path.relative_to(root)` in addition to inspecting path parts for `..`. This prevents symlink bypasses and prefix collisions (e.g. `/tmp/ws_outside` starting with `/tmp/ws`).
2. **Deterministic Credential Purging:**
   - Instead of trying to maintain a blacklist of every cloud provider, `sanitize_environment` enforces an allowlist of base environment variables (`PATH`, `LANG`, `LC_ALL`, `PYTHONPATH`, `SYSTEMROOT`, `TMPDIR`) while screening any variable matching `(?i)(API_KEY|SECRET|TOKEN|PASS|PASSWORD|AUTH|CREDENTIAL|PRIVATE|AWS_|GITHUB_|GH_|HARNESS_)`.
3. **Orphan Process Prevention via Process Groups:**
   - In `RestrictedSubprocessSandbox`, subprocesses are launched under a new session (`os.setsid()`). On timeout expiration, `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` is dispatched to eradicate any child processes spawned by untrusted test runners before raising or returning timeout status.

---

## 5. Deviations & Blockers

- None. Minor adjustment made in `TestToolGatewaySandboxIntegration.setUp` to supply `TaskSubmitRequest` to `ledger.submit_task()`, in accordance with the `LedgerStore` API. All 76 tests pass with 0 errors.
