# Phase 06 Plan 01: WorkerDaemon Architecture, Heartbeat Loop, Server API & CLI `harness worker` Summary

**Execution Date:** 2026-11-01  
**Plan:** `.gsd/phases/06-distributed-worker-sync/06-01-PLAN.md`  
**Status:** Complete (3/3 tasks completed, 140/140 tests green)

---

## 1. Overview & Objectives

Plan 06-01 implemented the standalone distributed `WorkerDaemon` architecture, supporting both direct in-process ledger access (`DirectLedgerWorkerClient`) and remote HTTP REST transport (`RestWorkerClient`) with `X-Worker-Token` authentication. The daemon features host machine profiling (detecting Apple Silicon M4 / GPU / Server classes, CPU/memory, CoW backend, and local models), periodic background heartbeat loops for CAS lease renewals, atomic task queue polling, graceful termination on signals (`SIGINT`/`SIGTERM`) releasing active leases, and a dedicated CLI command `harness worker`.

---

## 2. Tasks Executed & Git Commits

| Task # | Task Description | Files Touched | Git Commit |
|---|---|---|---|
| **Task 1** | Implement Lease Renewal, Worker Heartbeat & REST API Worker Endpoints | `src/harness_core/db.py`, `src/harness_core/server.py` | `8164909` (`feat(phase-06): implement lease renewal, worker heartbeat and REST worker endpoints`) |
| **Task 2** | Implement WorkerDaemon Architecture and `harness worker` CLI Command | `src/harness_core/cow.py`, `src/harness_core/worker_daemon.py`, `src/harness_core/cli.py` | `1e362e2` (`feat(phase-06): implement WorkerDaemon architecture and harness worker CLI command`) |
| **Task 3** | Author Unit & Integration Test Suite for WorkerDaemon | `src/harness_core/db.py`, `src/harness_core/orchestrator.py`, `src/harness_core/server.py`, `src/harness_core/worker_daemon.py`, `tests/test_worker_daemon.py` | `b497785` (`feat(phase-06): author worker daemon and REST client test suite`) |

---

## 3. Implementation Details

1. ** authoritative Ledger Lease Management (`src/harness_core/db.py`):**
   - Implemented `renew_lease` and `renew_lease_async` with CAS fencing token validation against active task max token and released status, raising `ConcurrencyFenceError` on stale or superseded leases.
   - Enforced monotonic lease expiration advancement (`expires = max(now + ttl_seconds, cur_expires + 1)`).
   - Implemented `release_lease` and `release_lease_async` updating `released_at = now`.
   - Implemented `heartbeat_worker` and `heartbeat_worker_async` recording worker timestamp and health status.
   - Added `get_lease` and `get_worker` inspection helpers across SQLite WAL and PostgreSQL engines.
   - Enabled flexible dict payload submission in `submit_task` / `submit_task_async` defaulting to `QUEUED` state.

2. **REST API Server Endpoints & Authentication (`src/harness_core/server.py`):**
   - Added worker authentication support (`auth_token`) via case-insensitive `X-Worker-Token` and `Authorization: Bearer <token>` headers.
   - Implemented worker routes: `POST /v1/workers/register` and `POST /v1/workers/heartbeat`.
   - Implemented atomic claim route: `POST /v1/tasks/claim` returning claimed task or `{"task": None}`.
   - Implemented lease control routes: `POST /v1/leases/{lease_id}:renew` and `POST /v1/leases/{lease_id}:release`.
   - Mapped `ConcurrencyFenceError` to HTTP 409 Conflict.

3. **Distributed Worker Architecture (`src/harness_core/worker_daemon.py`):**
   - `inspect_machine_profile`: Discovers hardware architecture, Darwin ARM64 (`m4_air`), GPU presence, CoW snapshot backend, memory/CPU cores, and local LLM models.
   - `WorkerClient` Protocol: Defines unified interface for worker operations.
   - `DirectLedgerWorkerClient`: Zero-network local ledger client executing via `HarnessOrchestrator`.
   - `RestWorkerClient`: Remote HTTP client utilizing standard library `urllib.request` with automatic HTTP 409 -> `ConcurrencyFenceError` and HTTP 401 -> `PermissionError` translation.
   - `WorkerDaemon`:
     - Background thread `_heartbeat_loop` running at `max(0.2, lease_ttl / 3.0)` to maintain lease validity.
     - Detects preemption/fencing conflicts and sets `self.fenced = True`.
     - Polling claim loop `_claim_loop` with `--once` execution mode support.
     - Signal handlers for `SIGINT` / `SIGTERM` invoking graceful lease release and thread shutdown.

4. **CLI Subcommand Integration (`src/harness_core/cli.py`):**
   - Registered `harness worker` with `--worker-id`, `--machine-class`, `--poll-interval`, `--lease-ttl`, `--server-url`, `--token`, and `--once` arguments.

5. **Orchestrator Attempt Reuse (`src/harness_core/orchestrator.py`):**
   - Enhanced `execute_task`, `_execute_fast_path`, and `_execute_standard_dag` with optional `lease_info` reuse to avoid creating duplicate attempts on claimed tasks.

---

## 4. Empirical Verification Evidence

1. **Task 1 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.db import LedgerStore; db = LedgerStore(':memory:'); w = db.register_worker('w1'); t = db.submit_task({'goal': 'test'}); claimed = db.claim_next_task('w1'); renewed = db.renew_lease(claimed['lease_id'], claimed['fencing_token'], ttl_seconds=60); released = db.release_lease(claimed['lease_id']); print('OK', renewed['expires_at'] > claimed['expires_at'], released['released_at'] is not None)"
   ```
   **Output:** `OK True True`

2. **Task 2 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.worker_daemon import WorkerDaemon, DirectLedgerWorkerClient, inspect_machine_profile; from harness_core.orchestrator import HarnessOrchestrator; import tempfile; d = tempfile.mkdtemp(); orch = HarnessOrchestrator(d); client = DirectLedgerWorkerClient(orch); daemon = WorkerDaemon(client, worker_id='test-worker', machine_class='m4_air', lease_ttl=10); prof = inspect_machine_profile(); print('DAEMON_INITIALIZED', daemon.worker_id, prof['machine_class'], prof['cow_backend'])"
   ```
   **Output:** `DAEMON_INITIALIZED test-worker m4_air apfs_clone`

3. **Task 3 Unit & Integration Test Suite (`tests/test_worker_daemon.py`):**
   ```bash
   .venv/bin/python -m unittest tests/test_worker_daemon.py -v
   ```
   **Output:**
   ```
   test_fencing_detected_in_heartbeat_loop ... ok
   test_graceful_shutdown_releases_lease ... ok
   test_heartbeat_loop_and_lease_renewal ... ok
   test_machine_profiling_and_registration ... ok
   test_rest_client_and_server_parity ... ok
   test_rest_client_auth_rejection ... ok
   test_rest_client_concurrency_fencing_conflict ... ok
   test_single_task_execution_mode_once ... ok
   ----------------------------------------------------------------------
   Ran 8 tests in 4.252s
   OK
   ```

4. **Complete Repository Test Suite:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -p "test_*.py"
   ```
   **Output:**
   ```
   Ran 140 tests in 8.879s
   OK
   ```

---

## 5. Deviations & In-Scope Bug Fixes

- **Rule 1 (Bug Fix): Dict Task Submission Handling:** `submit_task` in `src/harness_core/db.py` was adapted to accept dictionary inputs directly (in addition to `TaskSubmitRequest` dataclasses) and default dictionary submissions to `QUEUED` so tasks submitted directly in unit test scripts are immediately claimable.
- **Rule 1 (Bug Fix): Monotonic Lease Renewal:** `renew_lease` was updated to guarantee monotonic extension even when renewed with a TTL shorter than the remaining lease duration (`max(now + ttl_seconds, cur_expires + 1)`).
- **Rule 1 (Bug Fix): Orchestrator Claimed Attempt Reuse:** `execute_task` in `src/harness_core/orchestrator.py` was updated to accept `lease_info` so tasks claimed by `WorkerDaemon` reuse the atomic claim attempt rather than generating a redundant second attempt.
- **Rule 1 (Bug Fix): Hermetic REST Client Testing:** Provided a mock `urlopen` dispatcher in `tests/test_worker_daemon.py` routing directly to `HarnessAPIApp.handle_request`, allowing exhaustive testing of REST serialization, authentication, and HTTP error code translation hermetically within the sandbox.

---

## 6. Next Steps

- Proceed to Phase 06 Plan 02 (`06-02-PLAN.md`): Cross-Machine CoW Delta Archive Sync, Lease Expiry Handover & Monotonic Fencing Defense.
