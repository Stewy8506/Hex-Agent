# Plan 05-01: Multi-Turn Autonomous Repair Loop & Retry Escalation — Summary

**Execution Date:** 2026-10-31  
**Phase:** 05 (Multi-Turn Autonomous Repair & Dynamic DAG Replanning)  
**Plan:** 05-01 (Wave 1)  
**Status:** Complete  

---

## 1. Summary of Completed Work

### Task 1: Multi-Turn Autonomous Repair Loop in HarnessOrchestrator
- Enhanced `HarnessOrchestrator.__init__` in `src/harness_core/orchestrator.py` to accept an optional `repair_patch_generator: Callable[..., list[dict[str, Any]]] | None = None` for deterministic testing, custom strategies, and model router dispatch.
- Implemented `generate_repair_patch(task_id, attempt_ws, failure_digest, blast_report, modified_files)`:
  - Supports custom/mock patch generators with flexible signatures.
  - Automatically extracts candidate failing source files from failure tracebacks and compressed digests.
  - Assembles prompt incorporating Tier-1 compressed failure digest, Active Graphify AST blast-radius warnings, and source code context.
  - Formulates structured request for `write_file` tool actions and executes routed request via `self.router.execute_routed_request` with `task_class="repair"`, `risk_class="R2"`, and task budget limits.
  - Implemented resilient fallback parsing (`_parse_patch_operations`) for markdown JSON arrays and raw python code blocks.
- Refactored `_execute_standard_dag` verification pipeline:
  - On verification failure (`not ver_outcome["passed"]`), initiates iterative bounded repair loop bounded by `max_repair_retries` (`TaskBudget.max_retries_per_step` or constraint overrides).
  - Iterates `VERIFYING -> RUNNING` (reason: `AUTONOMOUS_REPAIR_ATTEMPT`) -> applies concrete repair patch operations to isolated `attempt_ws` via `self.gateway.execute_code_mode_composition` -> `RUNNING -> VERIFYING` (reason: `REPAIR_VERIFICATION_ATTEMPT`) -> re-executes verification.
  - On passing repair, synchronizes verified changes to base workspace `source_ws`, records episodic memory summary, and transitions to `SUCCEEDED` (reason: `PROOF_VERIFIED`, recording proof ID and repair attempt count).

### Task 2: Retry Budget Ceiling, Forensic CoW Preservation & Escalation to NEEDS_ATTENTION
- Hardened failure handling when retries are exhausted or task budget ceilings are breached:
  - If retries are exhausted (`repair_attempt >= max_repair_retries`):
    - Sets `failure_class = "REPAIR_RETRIES_EXHAUSTED"` and `reason_code = "REPAIR_RETRIES_EXHAUSTED"`.
  - If budget ceiling is breached (`TaskBudget.max_cost_usd` exceeded or `BudgetExceededError` raised):
    - Sets `failure_class = "BUDGET_EXCEEDED"` and `reason_code = "BUDGET_EXHAUSTED"`.
  - Freezes dirty `attempt_ws` state by creating an immutable forensic CoW snapshot (`{task_id}_repair_exhausted_att{repair_attempt}`).
  - Persists forensic checkpoint in ledger recording pending steps, completed steps, tree SHA, and snapshot path.
  - Updates attempt record with `FAILED` status and failure classification.
  - Safely transitions task state to `NEEDS_ATTENTION` with terminal reason and comprehensive payload (including `forensic_snapshot_path`, `verification_results`, `repair_attempts`).
  - Base project workspace `source_ws` is guaranteed to remain pristine and uncorrupted on repair failure.
- Updated `src/harness_core/models.py`:
  - Enhanced `TaskBudget.from_dict` to accept existing `TaskBudget` instances transparently.
  - Documented retry ceiling (`max_retries_per_step = 2`) and cost limit (`max_cost_usd = 2.0`) constraints.
  - Confirmed state machine permits `TaskState.VERIFYING -> TaskState.NEEDS_ATTENTION`, `TaskState.VERIFYING -> TaskState.RUNNING`, and `TaskState.RUNNING -> TaskState.NEEDS_ATTENTION`.
- Hardened `src/harness_core/db.py`:
  - Updated `transition_task` and `transition_task_async` to preserve `terminal_reason` when explicitly provided on state transitions (including non-terminal interrupts like `NEEDS_ATTENTION`).

### Task 3: Autonomous Repair Benchmark and Retry Escalation Test Suite
- Authored comprehensive test suite in `tests/test_autonomous_repair.py` covering:
  - **Happy Path Autonomous Repair:** Broken calculator defect (`a - b` instead of `a + b`) triggers test failure, dense log compression, state cycling `VERIFYING -> RUNNING -> VERIFYING`, patch application, passing re-verification, signed Proof-Carrying Artifact bundle saved to CAS with diff, and clean sync to base workspace.
  - **Retry Exhaustion Escalation:** Non-working patches trigger exactly 2 repair attempts, safe transition to `NEEDS_ATTENTION`, valid forensic CoW snapshot preserved on disk, and base workspace left untouched.
  - **Compound Multi-Failure Repair:** Handles defects across multiple modules (`math_ops.py` and `string_ops.py`), repairing turn-by-turn with Active Graphify blast-radius test pinning.
  - **Hard Budget Limit Escalation:** Cost ceiling triggers immediate escalation to `NEEDS_ATTENTION` with `BUDGET_EXHAUSTED` and forensic snapshot before patch execution.
  - **Dynamic Injected BudgetExceededError:** Runtime token/cost breach during patch generation safely freezes workspace and escalates to `NEEDS_ATTENTION`.
  - **Router Patch Generation & Fallback Parsing:** Validates JSON array parsing and markdown code block extraction.
- Full test suite verified: 127/127 tests green (100% passing).

---

## 2. Verification Evidence

1. **Task 1 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; import tempfile; o = HarnessOrchestrator(tempfile.mkdtemp()); print('Orchestrator repair methods:', hasattr(o, 'generate_repair_patch'))"
   # Output: Orchestrator repair methods: True
   ```

2. **Task 2 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.models import TaskState, VALID_TRANSITIONS, TaskBudget; b = TaskBudget(); assert TaskState.NEEDS_ATTENTION in VALID_TRANSITIONS[TaskState.VERIFYING]; assert TaskState.RUNNING in VALID_TRANSITIONS[TaskState.VERIFYING]; print('Transitions verified')"
   # Output: Transitions verified
   ```

3. **Task 3 Verification:**
   ```bash
   .venv/bin/python -m unittest tests/test_autonomous_repair.py -v
   # Output: Ran 6 tests in 0.620s -> OK (100% passing, 0 failures, 0 errors)
   ```

4. **Full Test Suite Regression Run:**
   ```bash
   .venv/bin/python -m unittest discover -s tests -p "test_*.py"
   # Output: Ran 127 tests in 4.204s -> OK (100% passing, 0 failures, 0 errors)
   ```

---

## 3. Atomic Commits

- Task 1: `44f9289 feat(phase-05): implement multi-turn autonomous repair loop in orchestrator`
- Task 2: `dd7e7e8 feat(phase-05): implement retry budget ceiling and forensic snapshot escalation to NEEDS_ATTENTION`
- Task 3: `d1f8eee feat(phase-05): author autonomous repair benchmark and retry escalation test suite`

---

## 4. Deviations & Blockers

- Minor deviation: Hardened `src/harness_core/db.py` to preserve `terminal_reason` when passed into `transition_task` / `transition_task_async` for `NEEDS_ATTENTION` state transitions, ensuring detailed diagnosis reasons are permanently retained on the task record.
- No blockers. All must-haves confirmed and verified.
