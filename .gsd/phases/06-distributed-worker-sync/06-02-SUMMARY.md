# Phase 06 Plan 02: Cross-Machine CoW Delta Archive Sync, Lease Expiry Handover & Fencing Defense Summary

**Execution Date:** 2026-11-01  
**Plan:** `.gsd/phases/06-distributed-worker-sync/06-02-PLAN.md`  
**Status:** Complete (3/3 tasks completed, 152/152 tests green)

---

## 1. Overview & Objectives

Plan 06-02 delivered cross-machine Copy-on-Write (CoW) delta archive mobility, automated lease expiration handover, 0ms tool journal replay, and monotonic fencing token defense. When tasks execute across distributed worker nodes, workspace checkpoints and step outputs are exported into deterministic `.tar.gz` delta archives and registered as Content-Addressed Store (CAS) artifacts. When a worker loses connectivity, sleeps, or crashes mid-task, its active lease expires and is reaped; a second worker claims the task queue with a monotonically incremented fencing token (e.g. 1 -> 2), reconstructs the remote workspace from the latest CAS delta archive, validates the Merkle tree SHA, replays already-sealed tool calls in 0ms (< 15ms) directly from the call journal, and completes execution. If the stale original worker wakes up and attempts any tool call, lease mutation, or state transition with its stale fencing token, the operation is rejected with `ConcurrencyFenceError` locally and HTTP 409 Conflict over REST, guaranteeing zero split-brain writes, duplicate executions, or lost progress.

---

## 2. Tasks Executed & Git Commits

| Task # | Task Description | Files Touched | Git Commit |
|---|---|---|---|
| **Task 1** | Implement Cross-Machine CoW Delta Archive Export/Import & CAS Artifact Transfer | `src/harness_core/cow.py`, `src/harness_core/server.py`, `src/harness_core/orchestrator.py`, `src/harness_core/worker_daemon.py`, `src/harness_core/db.py`, `src/harness_core/gateway.py` | `3307303` (`feat(phase-06): implement CoW delta archive import, CAS artifact transfer and workspace reconstruction`) |
| **Task 2** | Implement Lease Expiry Handover, 0ms Journal Replay & Monotonic Fencing Defense | `src/harness_core/db.py`, `src/harness_core/models.py`, `tests/test_postgres_concurrency.py` | `4fe5938` (`feat(phase-06): implement lease expiry handover and monotonic fencing token defense`) |
| **Task 3** | Author Comprehensive Test Suite for Cross-Machine CoW Sync & Fencing Defense | `src/harness_core/cow.py`, `src/harness_core/db.py`, `tests/test_cross_machine_cow_sync.py` | `7500c9c` (`feat(phase-06): author cross-machine CoW delta sync and fencing defense test suite`) |

---

## 3. Implementation Details

1. **CoW Delta Archive Import/Export with Tar-Slip Defense (`src/harness_core/cow.py`):**
   - Added `import_delta_archive` to `WorkspaceSnapshotProvider` protocol, `APFSAndPortableCoWProvider`, `OverlayFSSnapshotProvider`, and `BtrfsSnapshotProvider`, along with standalone module functions `export_delta_archive` and `import_delta_archive`.
   - Built strict tar-slip traversal protection inspecting all `tarinfo` member paths, rejecting any archive entries containing `..` path segments or resolving outside the target workspace with `ValueError`.
   - Handled both flat archives and archives containing a root `workspace/` folder directly unpacking into the target workspace.
   - Cleans pre-existing non-ignored files in target workspace prior to unpacking to ensure 100% deterministic Merkle tree SHA parity.

2. **CAS Artifact REST Endpoints & Worker Transfer Client (`src/harness_core/server.py`, `src/harness_core/worker_daemon.py`):**
   - `POST /v1/artifacts/delta`: Accepts raw `.tar.gz` gzip streams or JSON with base64 payload, stores bundle deterministically in `<data_dir>/cas/<sha256>.tar.gz`, and records the artifact in the ledger with `kind="cow_delta_archive"`.
   - `GET /v1/artifacts/{artifact_id}/file`: Streams raw archive bytes with `Content-Type: application/gzip` and `Content-Disposition`.
   - Added `POST /v1/tasks/{task_id}:transition` supporting fencing token validation and HTTP 409 Conflict mapping.
   - Enhanced `DirectLedgerWorkerClient` and `RestWorkerClient` with `list_artifacts`, `download_artifact`, and `upload_delta_artifact`.

3. **Orchestrator Remote Workspace Export and Reconstruction (`src/harness_core/orchestrator.py`):**
   - Implemented `export_workspace_delta(task_id, attempt_ws)` exporting workspace state to CAS delta archive and updating `active_workspace_tree_sha`.
   - Automatically exports workspace delta archives upon step completion and approval/forensic checkpoints.
   - Implemented `reconstruct_workspace_from_delta(task_id, target_ws)` unpacking latest delta bundle and verifying `reconstructed_tree_sha == task["active_workspace_tree_sha"]`, raising `ValueError` on any mismatch.
   - Updated `_execute_standard_dag` to reconstruct workspace from CAS delta archives upon failover claim and pass `fencing_token` into gateway tool invocations.

4. **Lease Expiry Reaping, Takeover, and Monotonic Fencing Defense (`src/harness_core/db.py`):**
   - Added `reap_expired_leases` (sync and async) finding all tasks in `RUNNING` or `VERIFYING` whose leases have expired (`released_at IS NULL AND expires_at < now`), releasing the expired leases and transitioning tasks back to `QUEUED` with `reason_code="LEASE_EXPIRED"`.
   - In `claim_next_task` / `claim_next_task_async`, automatically calls `reap_expired_leases`, queries `max_tok = MAX(fencing_token)`, and assigns `next_token = max_tok + 1` (e.g. 1 -> 2).
   - Records `LEASE_TAKEOVER` hash-chained task events capturing `prior_fencing_token`, `new_fencing_token`, `worker_id`, and `lease_id`.
   - Hardened `begin_tool_call`, `seal_tool_call`, `transition_task`, `renew_lease`, and `release_lease` against stale fencing tokens (`fencing_token < max_tok`), raising `ConcurrencyFenceError`.

5. **0ms Tool Journal Replay (`src/harness_core/gateway.py`):**
   - Propagated active `fencing_token` from orchestrator into `ToolGateway.invoke` and `execute_code_mode_composition`.
   - Replays previously sealed tool calls in 0ms (< 15ms) directly from the call journal (`_replayed == True`, `replayed_from_call_id`), without invoking external tools or subprocesses.

---

## 4. Empirical Verification Evidence

1. **Task 1 Verification:**
   ```bash
   python3 -c "from harness_core.cow import get_default_snapshot_provider, import_delta_archive; import tempfile, pathlib; p1 = pathlib.Path(tempfile.mkdtemp()); (p1 / 'test.txt').write_text('hello cow'); cow = get_default_snapshot_provider(p1 / 'snaps'); snap = cow.create_snapshot(p1, 'chk1'); arc = p1 / 'delta.tar.gz'; cow.export_delta_archive(snap.snapshot_path, arc); p2 = pathlib.Path(tempfile.mkdtemp()); sha2 = import_delta_archive(arc, p2); print('RESTORE_VERIFIED', (p2 / 'test.txt').read_text() == 'hello cow', sha2 == snap.workspace_tree_sha)"
   ```
   *Output:*
   `RESTORE_VERIFIED True True`

2. **Task 2 Verification:**
   ```bash
   .venv/bin/python -c "
   from harness_core.db import LedgerStore, ConcurrencyFenceError
   import time
   db = LedgerStore(':memory:')
   t = db.submit_task({'goal': 'fence test'})
   c1 = db.claim_next_task('w1', ttl_seconds=1)
   time.sleep(1.2)
   reaped = db.reap_expired_leases()
   c2 = db.claim_next_task('w2')
   print('FENCING_TOKENS', c1['fencing_token'], c2['fencing_token'])
   try:
       db.transition_task(t['task_id'], 'SUCCEEDED', fencing_token=c1['fencing_token'])
       print('FAIL: stale token accepted')
   except ConcurrencyFenceError:
       print('SUCCESS: stale token rejected')
   "
   ```
   *Output:*
   `FENCING_TOKENS 1 2`
   `SUCCESS: stale token rejected`

3. **Task 3 Verification (`tests/test_cross_machine_cow_sync.py`):**
   ```bash
   .venv/bin/python -m unittest tests/test_cross_machine_cow_sync.py -v
   ```
   *Output:*
   ```text
   test_clean_target_directory_before_unpack ... ok
   test_cross_machine_cow_export_and_import_integrity ... ok
   test_import_with_workspace_root_prefix_and_flat_archive ... ok
   test_cross_machine_handover_with_0ms_journal_replay ... ok
   test_lease_expiry_reaping_and_monotonic_fencing_increment ... ok
   test_upload_and_download_delta_artifact_over_rest ... ok
   test_stale_worker_rejected_on_all_control_plane_mutations ... ok
   test_stale_worker_rest_api_rejection_http_409 ... ok
   test_corrupted_archive_rejected ... ok
   test_tar_slip_nested_traversal_rejected ... ok
   test_tar_slip_relative_path_traversal_rejected ... ok
   test_workspace_reconstruction_tree_sha_mismatch_raises_error ... ok

   Ran 12 tests in 5.095s
   OK
   ```

4. **Complete Repository Test Suite:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
   ```
   *Output:*
   ```text
   Ran 152 tests in 14.017s
   OK
   ```
   All 152 unit, integration, and contract tests across all 6 phases pass with zero errors, zero failures, and zero regressions.

---

## 5. Next Steps

- Phase 06 is fully complete. All must-haves for distributed worker sync, CAS delta archives, lease expiry handover, 0ms journal replay, and monotonic fencing defense have been verified with empirical proof.
- Ready to proceed to milestone review or subsequent phases.
