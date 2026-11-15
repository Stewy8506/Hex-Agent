# Phase 33 Plan 33-03 Summary: Durable Lifecycle & Idle Reflection Workers (Fixes F-02, F-05, F-06)

**Date:** 2026-11-15  
**Phase:** 33 (Production Cognition Hardening & Execution Seams)  
**Plan:** 33-03  
**Status:** COMPLETE (3/3 Tasks Completed & Empirically Verified)  
**Commits:**
- `1b4fd36`: `feat(phase-33): archive-before-delete durability, fts idempotency, and lifecycle scheduling (F-02)`
- `6315ca8`: `feat(phase-33): transactional multi-operation memory execution and verified deletion (F-05)`
- `385da8c`: `feat(phase-33): idle reflection drain worker and orchestrator idle execution (F-06)`

---

## 1. Objectives & Executive Overview

Plan 33-03 closed three critical production cognition findings from `audit.md`:
1. **F-02 (Memory Lifecycle & Archive Durability):** Eliminated data loss during demotion by enforcing strict archive-before-delete; hardened SQLite cold storage (`archive.db`) with unique constraints and FTS5 deduplication; wired automatic promotion checks into task completion and lifecycle sweeps.
2. **F-05 (Transactional Multi-Operation Memory Execution & Deletion Verification):** Transformed `CuratedMemoryStore.execute_operations()` from independent sequential disk writes into an all-or-nothing transactional engine validating staging states, budget ceilings, and text matching before committing, with multi-file write rollback; guarded `AMEMController.delete_memory()` against disk deletion failures to prevent metadata drift.
3. **F-06 (Idle Reflection Worker):** Solved the orphaned reflection candidate backlog by implementing `process_pending_reflections()` and `check_and_run_idle_workers()`, which drain `.hex/reflections/pending_reflections.json` during idle intervals under file locks, synthesizing lessons into `LESSONS.md`, updating guardrails, registering lifecycle reflections, and enforcing a 3-strike retry ceiling.

---

## 2. Tasks Delivered

### Task 1: Archive-Before-Delete, FTS Idempotency, and Lifecycle Scheduling (F-02)
- **Files Modified:** `src/harness_core/memory.py`, `src/harness_core/orchestrator.py`, `src/harness_core/router.py`, `tests/test_durable_lifecycle_and_reflection.py`
- **Changes:**
  - `MemoryLifecycleManager.archive_entry()`: Wrapped in explicit SQLite transaction context (`with conn:`); cleans prior matching rows from `archived_memories_fts` (`DELETE FROM archived_memories_fts WHERE memory_id = ?`) before re-inserting to prevent FTS row duplication on retry; returns `True` only after transaction commit.
  - `MemoryLifecycleManager.run_demotion_sweep()`: Evaluates `archived_ok = self.archive_entry(...)`. If archival fails, logs error and retains active note in `MEMORY.md`/`USER.md` without invoking controller deletion.
  - `HarnessOrchestrator._execute_standard_dag()` & `run_agent_loop()`: Integrated automatic promotion check (`check_promotions()`) at successful task completion, promoting high-confidence reflections ($\ge 3$ hits) to `MEMORY.md`.
  - Added `run_lifecycle_maintenance(workspace_root, retention_days=30)` helper and orchestrator method.
  - Added `GovernedMemoryService.lifecycle_mgr` property in `src/harness_core/router.py`.
- **Commit:** `1b4fd36`

### Task 2: Transactional Batch Operations and Verified Physical Deletion (F-05)
- **Files Modified:** `src/harness_core/memory.py`, `tests/test_durable_lifecycle_and_reflection.py`
- **Changes:**
  - `CuratedMemoryStore.execute_operations()`: Implemented multi-file locking in deterministic sorted order via `ExitStack`; performs drift checks across all target files; creates in-memory staging copies and rollback snapshots; validates all operations (`add`, `replace`, `patch`, `remove`) against character limits and text matches; writes only after 100% of operations pass validation; automatically restores original file contents if any multi-file disk write fails.
  - `AMEMController.delete_memory()` & `process_memory(action="DELETE")`: Checked `self.store.remove(...)` return dictionary; if physical disk removal fails (`not res.get("ok")`), metadata in `self._items` and `self._tag_index` is strictly preserved and an error dictionary is returned.
- **Commit:** `6315ca8`

### Task 3: Idle Reflection Worker Draining Queue (F-06)
- **Files Modified:** `src/harness_core/orchestrator.py`, `tests/test_durable_lifecycle_and_reflection.py`
- **Changes:**
  - `_handle_post_task_reflection()`: Added `final_patch`, `retry_count: 0`, and file-locking protection (`_file_lock`) when appending suppressed reflection candidates to `pending_reflections.json`.
  - `process_pending_reflections()`: Drains queued pending reflections under file lock when system is idle (`is_idle=True`); synthesizes lessons via `ReflectionSynthesizer`; updates `LESSONS.md`, memory lifecycle reflections, and negative guardrails; tracks candidate status (`processing` -> `completed` / `failed`); enforces a 3-strike retry ceiling.
  - `HarnessOrchestrator.check_and_run_idle_workers()`: Exposed idle maintenance hook.
- **Commit:** `385da8c`

---

## 3. Verification & Empirical Proof

### Test Suite Results:
- `tests/test_durable_lifecycle_and_reflection.py`: 8/8 tests PASSED (1.17s)
  - `test_archive_before_delete`: Verified active memory retained on archival failure.
  - `test_archive_idempotency`: Verified re-archiving memory does not duplicate rows in `archived_memories_fts`.
  - `test_promotion_wiring`: Verified reflection with $\ge 3$ references automatically promotes on task completion.
  - `test_batch_transaction_rollback`: Verified failure in Op 2 prevents any disk write of Op 1.
  - `test_delete_memory_disk_check`: Verified failed physical removal retains controller items and tag index.
  - `test_idle_reflection_worker`: Verified idle worker drains pending candidates into `LESSONS.md` and marks `completed`.
  - `test_idle_gate`: Verified non-idle invocation does not drain when policy is `idle`.
  - `test_retry_ceiling`: Verified candidates failing 3 times are marked `failed` and skipped.
- Full focused audit suite: 111/111 tests PASSED (9.74s)
- Zero syntax errors, compilation errors, or git diff formatting warnings (`git diff --check` clean, `compileall` clean).

---

## 4. Preserved Strengths Confirmed
- `PromptBuilder` maintains strict 5-slot assembly order.
- Memory snapshot immutability within active sessions is preserved.
- Negative guardrails invariant capture and AST-scoped retrieval remain untouched.
- Deterministic fallback for reflection synthesis operates without external cloud dependencies.
