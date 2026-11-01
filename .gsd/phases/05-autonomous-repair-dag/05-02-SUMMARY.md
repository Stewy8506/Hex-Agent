# Plan 05-02: Dynamic DAG Replanning & Autonomous Repair Benchmark — Summary

**Execution Date:** 2026-10-31  
**Phase:** 05 (Multi-Turn Autonomous Repair & Dynamic DAG Replanning)  
**Plan:** 05-02 (Wave 2)  
**Status:** Complete  

---

## 1. Summary of Completed Work

### Task 1: Implement Dynamic DAG Replanning and Selective Subgraph Invalidation
- Implemented `HarnessOrchestrator.replan_dag` in `src/harness_core/orchestrator.py`:
  - **Downstream Dependency Analysis:** Constructs directed child adjacency graph from `node.get("depends_on")` and performs breadth-first search (BFS) reachability traversal from `mutated_step_id` to identify all transitive downstream dependent step IDs.
  - **File Overlap & Target Detection:** Checks if any step's inputs, targets, or side effects (`path`, `target`, `file`, `targets`, `inputs`, `files`, `side_effects`) overlap with `actual_modified_files` or blast radius tests, adding affected steps and their downstream children to the invalidation set.
  - **Selective Subgraph Invalidation:** Selectively marks only downstream dependent steps as `INVALIDATED` and removes them from `completed_step_ids` for re-execution. Preserves independent completed steps as `COMPLETED` without re-running them.
  - **Dynamic Blast-Radius Test Pinning:** Interrogates Active Graphify (`self.graphify.compute_blast_radius`), extracts newly discovered `impacted_tests`, and merges them into `blast_radius_checks`.
  - **Monotonic Plan Revisions:** Saves new plan with monotonically incremented `revision` and sets `supersedes_revision` to previous revision.
  - **Cryptographically Hash-Chained Task Event:** Records `PLAN_REVISED` task event with reason `SELECTIVE_SUBGRAPH_INVALIDATION`, active workspace Merkle tree SHA, and structured payload detailing mutated, invalidated, and preserved step IDs alongside pinned tests.
- Refactored `_execute_standard_dag` in `src/harness_core/orchestrator.py`:
  - Converted sequential step iteration to a dynamic DAG dependency resolver loop (`while True`) that picks ready nodes whose dependencies are all satisfied.
  - Added unexpected side-effects detection comparing actual modified files against declared `side_effects` (`actual - declared`).
  - Automatically triggers `replan_dag` when unexpected file modifications occur, when explicit `trigger_replan` is requested, or when Active Graphify discovers unpinned downstream tests/callers.
- Enhanced `src/harness_core/db.py`:
  - Added `PlanNode` object serialization support to `save_plan` and `save_plan_async`.
  - Implemented `get_plan_revision`, `get_plan_revision_async`, `list_plans`, and `list_plans_async` for complete dual-driver (SQLite & PostgreSQL) plan versioning.

### Task 2: Author Dynamic DAG Replanning and Selective Invalidation Test Suite
- Authored comprehensive test suite in `tests/test_dag_replanning.py` covering:
  - **Selective Invalidation with Independent Step Preservation:** Proves that mutating a branch step (2A) marks its downstream dependent (Step 3) as `INVALIDATED` while preserving independent completed steps (Step 1 and Step 2B) as `COMPLETED`. Verifies `revision == 2` and `supersedes_revision == 1`.
  - **Hash-Chained Task Event for Plan Revisions:** Validates sequential cryptographic hash chaining across all task events leading to and including `PLAN_REVISED`.
  - **Dynamic Test Pinning via Active Graphify:** Proves that when a step modifies `core_lib.py`, Active Graphify discovers its downstream caller `test_core_callers.py` and pins it to `blast_radius_checks`, which is then enforced during proof verification.
  - **End-to-End Task Execution with Dynamic Replanning:** Executes a full task via `orchestrator.execute_task` where a middle step modifies a file unexpectedly, triggers dynamic DAG replanning, executes the invalidated downstream step with updated file state, and completes successfully with `SUCCEEDED`.
  - **Multi-Branch Selective Invalidation:** Proves that in a multi-branch DAG (`root -> A1 -> A2` and `root -> B1 -> B2`), mutating A1 invalidates only A2 while preserving Root, B1, and B2.

### Task 3: Full Verification & Regression Test Suite Across All Modules
- Executed unit and integration tests:
  - `python3 -m unittest tests/test_autonomous_repair.py` (6 tests passing).
  - `python3 -m unittest tests/test_dag_replanning.py` (5 tests passing).
- Executed repository-wide test discovery suite across all modules:
  - `python3 -m unittest discover -s tests -p "test_*.py" -v`
  - Total: 132 tests passing with zero failures and zero errors (100% green).

---

## 2. Verification Evidence

1. **Task 1 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; import tempfile; o = HarnessOrchestrator(tempfile.mkdtemp()); print('replan_dag exists:', hasattr(o, 'replan_dag'))"
   # Output: replan_dag exists: True
   ```

2. **Task 2 Verification:**
   ```bash
   .venv/bin/python -m unittest tests/test_dag_replanning.py
   # Output: Ran 5 tests in 0.215s -> OK (100% passing)
   ```

3. **Task 3 Repository Regression Verification:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -p "test_*.py" -v
   # Output: Ran 132 tests in 4.896s -> OK (100% passing, 0 failures, 0 errors)
   ```

---

## 3. Atomic Commits

- Task 1: `bb0612b feat(phase-05): implement dynamic DAG replanning and selective subgraph invalidation`
- Task 2: `71c1b71 feat(phase-05): author dynamic DAG replanning test suite with selective invalidation`

---

## 4. Deviations & Blockers

- Minor addition: Added `submit_task(req, auto_execute=False)` helper on `HarnessOrchestrator` to mirror `submit_and_run(req, auto_execute=auto_execute)`.
- No blockers. All must-haves confirmed and verified.
