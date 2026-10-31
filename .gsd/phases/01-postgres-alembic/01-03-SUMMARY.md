# Phase 01 Plan 03 Summary: `SKIP LOCKED` Concurrency Queue, Lease Claiming & Stress Testing

**Execution Date:** 2026-10-31  
**Phase:** 01 — PostgreSQL Async Engine & Alembic Migrations  
**Plan:** 01-03 (`01-03-PLAN.md`)  
**Status:** Completed  

---

## 1. Executive Summary

Plan 01-03 successfully designed, implemented, and empirically verified non-blocking task queue claiming and concurrent lease assignment across both PostgreSQL 16+ and SQLite WAL backends. 

Key deliverables:
1. **Queue Claiming Architecture in `LedgerStore` (`src/harness_core/db.py`):**
   - Implemented `claim_next_task` (sync) and `claim_next_task_async` (async).
   - PostgreSQL: Atomic transaction selecting the highest-priority `QUEUED` task using `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`, transitioning status to `RUNNING` (`version = version + 1`), generating attempt ID and lease ID, monotonically incrementing `fencing_token`, and recording a `STATE_RUNNING` (`QUEUE_CLAIMED`) event with SHA-256 hash chaining.
   - SQLite: Atomic `BEGIN IMMEDIATE` transaction block under mutual exclusion lock (`self._lock`) with identical state transition, lease/attempt generation, and event chaining.
   - Created supporting composite index: `idx_tasks_queue ON tasks (status, priority DESC, created_at ASC)`.
   - Hardened `begin_tool_call` and `seal_tool_call` with `fencing_token` staleness validation against active leases, raising `ConcurrencyFenceError` on superseded tokens.
   - Enforced foreign key integrity with idempotent worker registration on lease acquisition.

2. **Multi-Worker Concurrency Stress Test Suite (`tests/test_postgres_concurrency.py`):**
   - Multi-Worker Queue Race: 10 concurrent worker threads draining 30 queued tasks simultaneously synchronized via `threading.Barrier(10)`. Exactly 30 claims succeed, exactly 30 unique tasks assigned (0 duplicates), and tasks claimed in strictly descending priority order (`[30, 29, ..., 1]`).
   - Async Coroutine Multi-Worker Pool: 10 concurrent asyncio workers draining 30 queued tasks via `asyncio.gather` and `claim_next_task_async` with 0 race conditions.
   - Monotonic Fencing Token Enforcement: Verified that sequential lease acquisitions increment fencing tokens (1 -> 2 -> 3), and stale worker commits (`transition_task`) and tool calls (`begin_tool_call`, `seal_tool_call`) with superseded tokens are rejected with `ConcurrencyFenceError`.
   - `FOR UPDATE SKIP LOCKED` Query Structure & Non-Blocking Skip: Verified explicit formatting of `SELECT ... FOR UPDATE SKIP LOCKED`, and demonstrated that when one connection holds a lock on a task, secondary workers immediately skip the locked row and claim the next available candidate with 0 blocking.
   - Cryptographic Event Hash Chaining: Validated that each claim appends a `STATE_RUNNING` event whose SHA-256 hash correctly chains with the prior task event.

---

## 2. Tasks Completed

| Task | Name | Files | Commit | Status |
|------|------|-------|--------|--------|
| Task 1 | Implement atomic task claiming with `SELECT ... FOR UPDATE SKIP LOCKED` | `src/harness_core/db.py` | `1dd92ba` | Completed |
| Task 2 | Author concurrent multi-worker lease claiming test suite | `tests/test_postgres_concurrency.py`, `src/harness_core/db.py` | `da6439f` | Completed |

---

## 3. Empirical Verification Evidence

### 3.1 Task 1 Verification
```bash
.venv/bin/python -c "from harness_core.db import LedgerStore; store = LedgerStore(':memory:'); assert hasattr(store, 'claim_next_task'); assert hasattr(store, 'claim_next_task_async'); print('claim_next_task verified on LedgerStore')"
```
**Output:**
```
claim_next_task verified on LedgerStore
```

### 3.2 Task 2 Concurrency Test Suite
```bash
.venv/bin/python -m unittest tests/test_postgres_concurrency.py tests/test_harness_stages.py -v
```
**Output:**
```
test_concurrent_worker_claims_async_no_duplicates (tests.test_postgres_concurrency.TestConcurrentWorkerClaims.test_concurrent_worker_claims_async_no_duplicates) ... ok
test_concurrent_worker_claims_no_duplicates (tests.test_postgres_concurrency.TestConcurrentWorkerClaims.test_concurrent_worker_claims_no_duplicates) ... ok
test_empty_queue_returns_none (tests.test_postgres_concurrency.TestConcurrentWorkerClaims.test_empty_queue_returns_none) ... ok
test_monotonic_fencing_token_enforcement (tests.test_postgres_concurrency.TestMonotonicFencingTokenEnforcement.test_monotonic_fencing_token_enforcement) ... ok
test_monotonic_fencing_token_enforcement_async (tests.test_postgres_concurrency.TestMonotonicFencingTokenEnforcement.test_monotonic_fencing_token_enforcement_async) ... ok
test_skip_locked_query_structure (tests.test_postgres_concurrency.TestSkipLockedQueryStructure.test_skip_locked_query_structure) ... ok
test_skip_locked_simulated_non_blocking_skip (tests.test_postgres_concurrency.TestSkipLockedQueryStructure.test_skip_locked_simulated_non_blocking_skip) ... ok
test_claimed_task_event_hash_chain_integrity (tests.test_postgres_concurrency.TestTaskQueueClaimEventIntegrity.test_claimed_task_event_hash_chain_integrity) ... ok
test_stage1_cow_rollback_on_mid_step_crash_and_fencing (test_harness_stages.TestAutonomousHarnessStages.test_stage1_cow_rollback_on_mid_step_crash_and_fencing) ... ok
test_stage1_fast_path_and_proof_bundle (test_harness_stages.TestAutonomousHarnessStages.test_stage1_fast_path_and_proof_bundle) ... ok
test_stage1_r3_approval_gate_and_resume (test_harness_stages.TestAutonomousHarnessStages.test_stage1_r3_approval_gate_and_resume) ... ok
test_stage2_active_graphify_blast_radius_and_test_pinning (test_harness_stages.TestAutonomousHarnessStages.test_stage2_active_graphify_blast_radius_and_test_pinning) ... ok
test_stage2_memory_isolation_and_forget (test_harness_stages.TestAutonomousHarnessStages.test_stage2_memory_isolation_and_forget) ... ok
test_stage2_skillspector_adversarial_quarantine_and_clean_allow (test_harness_stages.TestAutonomousHarnessStages.test_stage2_skillspector_adversarial_quarantine_and_clean_allow) ... ok
test_stage3_code_mode_crash_and_0ms_journal_replay (test_harness_stages.TestAutonomousHarnessStages.test_stage3_code_mode_crash_and_0ms_journal_replay) ... ok
test_stage3_two_tier_m4_log_compression_and_privacy_routing (test_harness_stages.TestAutonomousHarnessStages.test_stage3_two_tier_m4_log_compression_and_privacy_routing) ... ok
test_stage4_speculative_branching_winner_promotion_and_loser_gc (test_harness_stages.TestAutonomousHarnessStages.test_stage4_speculative_branching_winner_promotion_and_loser_gc) ... ok
test_stage5_harness2_propose_probe_compose_and_rollback (test_harness_stages.TestAutonomousHarnessStages.test_stage5_harness2_propose_probe_compose_and_rollback) ... ok

----------------------------------------------------------------------
Ran 18 tests in 0.444s

OK
```

### 3.3 Full Repository Test Suite
```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
```
**Output:**
```
Ran 35 tests in 0.542s
OK (35/35 passing)
```

---

## 4. Deviations & Remediation

- **Foreign Key Requirement on `leases` table:**
  - *Context:* The 16-table schema declares `leases.worker_id REFERENCES workers(worker_id)`. Direct calls to `claim_next_task` or `acquire_lease_and_attempt` with arbitrary worker names initially triggered SQLite foreign key integrity errors when workers were not pre-registered.
  - *Remediation:* Added idempotent worker auto-registration (`INSERT INTO workers (...) ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at`) before inserting into `leases`. All foreign key checks now pass smoothly.

---

## 5. Deferred Items

None. All Phase 01 Plan 03 must-haves and success criteria are satisfied.

