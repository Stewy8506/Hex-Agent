# Phase 33 Plan 01 Summary: Execution Gates & Session Isolation (Fixes F-01, F-04, F-08)

**Execution Date:** 2026-11-15  
**Phase:** 33 (Production Cognition Hardening & Execution Seams)  
**Plan:** 33-01  
**Status:** COMPLETE (2/2 Tasks Done, Verified & Committed)  

---

## 1. Objectives & Executive Summary

Plan 33-01 addressed three critical execution boundary findings from `audit.md`:
1. **F-01 (Skill Verification Hooks):** Converted passive instructional skills into deterministic regression assertions by integrating sandboxed `verify:` execution, a clean minimal allowlisted environment, and SkillSpector candidate SHA-256 bundle hash checks into the orchestrator task completion flow.
2. **F-04 (KV-Cache Session Isolation):** Keyed Turn-0 memory snapshots by `(workspace, global_home, session_id)`, guaranteeing that an active session retains 100% frozen KV-cache stability while a fresh session hydrates latest memory without cross-session staleness.
3. **F-08 (Soul Budget & Provenance Hardening):** Reserved explicit space for `TRUNCATION_MARKER` so truncated soul text never exceeds `MAX_SOUL_CHARS` (2,500 chars / ~500 tokens), and introduced `SoulProvenance` (`workspace_user`, `global_user`, `repository`, `remote_profile`, `distribution`) so local workspace `.hex/SOUL.md` files are treated as user-authored (warning on heuristic injection matches instead of hard-blocking).

---

## 2. Tasks Completed & Commit Protocol

### Task 1: Wire deterministic skill `verify:` hooks into orchestrator task completion flow with sandbox execution, minimal environment, and SkillSpector hash verification (Fixes F-01)
- **Commit:** `943892c` — `feat(phase-33): wire sandboxed skill verification hooks and bundle hash checks into orchestrator (F-01)`
- **Key Changes:**
  - `src/harness_core/skillspector.py`:
    - Extracted `compute_bundle_sha256(bundle_dir)` as a standalone function and class method.
    - Added `get_approved_skill_hash(bundle_dir_or_name, ledger)` to resolve verified digests from the ledger database or bundle `audit.json`.
  - `src/harness_core/skills/executor.py`:
    - Defined `SecurityError(PermissionError)` for integrity and supply-chain violations.
    - Implemented `build_minimal_skill_env()` inheriting only safe system variables (`PATH`, `HOME`, `TMPDIR`, `USER`, `LANG`, `LC_ALL`, `TERM`) and explicitly stripping all AI provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), cloud credentials, database URLs, and SSH configs.
    - Updated `SkillExecutionEngine.run_skill_verification()`:
      - Validates candidate bundle hash against approved registration when `skill_bundle_dir` is provided, raising `SecurityError` on mismatch or unvetted status.
      - Confines command execution inside `sandbox.execute()` or `sandbox.run_command()` within the workspace root instead of unconfined host subprocesses.
    - Updated `assert_skill_regression_free()` with sandbox, bundle directory, and ledger support.
  - `src/harness_core/orchestrator.py`:
    - Added `_run_skill_verifications()` to match referenced skills (from constraints, metadata, or AST invariants touching modified files).
    - Wired skill verification into `_execute_standard_dag()` during task verification: if `must_pass: True` and the hook fails, records `SKILL_VERIFICATION_FAILED` in the ledger and marks `ver_outcome["passed"] = False`, blocking transition to `SUCCEEDED` and triggering autonomous repair.
  - `tests/test_skill_verification_wiring.py`:
    - Created 6-test suite verifying minimal env sanitization, sandbox containment, bundle tampering rejection, unapproved skill rejection, and orchestrator task gating.

### Task 2: Session-scoped Turn-0 memory snapshotting (`session_id`), exact `MAX_SOUL_CHARS` budget reservation, and explicit user-authored provenance for workspace souls (Fixes F-04, F-08)
- **Commit:** `d0fbacc` — `feat(phase-33): session-scoped memory snapshots, exact soul budgeting, and workspace soul provenance (F-04, F-08)`
- **Key Changes:**
  - `src/harness_core/memory.py`:
    - Updated `MemorySnapshot.build_frozen_snapshot()` and `CuratedMemoryStore.build_frozen_snapshot()` to accept `session_id`.
    - Keyed memory cache by `f"{ws}:{gh}:{session_id}"` when `session_id` is passed, isolating snapshots per session.
    - Added `reset_session_snapshot(session_id)` and `clear_all_snapshots()`.
    - Added `CuratedMemoryStore.get_frozen_turn0_snapshot(session_id)`.
  - `src/harness_core/soul.py`:
    - Fixed `_enforce_budget()`: calculates `keep_len = MAX_SOUL_CHARS - len(TRUNCATION_MARKER)` so total characters unconditionally remain `<= MAX_SOUL_CHARS` (2,500 chars).
    - Introduced `SoulProvenance(str, Enum)`: `GLOBAL_USER`, `WORKSPACE_USER`, `REPOSITORY`, `REMOTE_PROFILE`, `DISTRIBUTION`, with backwards-compatible string comparisons.
    - Updated `resolve_soul()` to tag local workspace `.hex/SOUL.md` as `SoulProvenance.WORKSPACE_USER`.
  - `src/harness_core/prompt_builder.py`:
    - Updated `build_system_prompt()` to accept `session_id` and `soul_provenance`.
    - Slot #1: Marks `user_authored=True` when provenance is `global_user` or `workspace_user`, warning on heuristic prompt injection patterns rather than hard-blocking.
    - Slot #4: Passes `session_id` to `MemorySnapshot.build_frozen_snapshot(..., session_id=session_id)`.
  - `src/harness_core/orchestrator.py`:
    - Passed `session_id=active_task_id` into `PromptBuilder.build_system_prompt()` in `run_agent_loop`.
  - `tests/test_session_isolated_snapshots.py`:
    - Created 4-test suite verifying session-isolated frozen snapshots, disk mutation isolation between Session A and B, exact soul budget caps, and workspace soul user-authored provenance.

---

## 3. Empirical Verification Evidence

### Test Suite Execution
```bash
.venv/bin/pytest tests/test_skill_verification_wiring.py tests/test_session_isolated_snapshots.py -v
```

Output:
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.2.0, pluggy-1.7.0 -- /Users/anv./Development/Harness/.venv/bin/python3.14
cachedir: .pytest_cache
rootdir: /Users/anv./Development/Harness
configfile: pyproject.toml
plugins: anyio-4.16.0
collecting ... collected 10 items                                                             

tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_minimal_environment_strips_secrets_and_provider_keys PASSED [ 10%]
tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_orchestrator_gates_task_completion_on_skill_verification PASSED [ 20%]
tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_run_skill_verification_executes_with_minimal_env PASSED [ 30%]
tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_run_skill_verification_in_sandbox PASSED [ 40%]
tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_tampered_skill_bundle_triggers_hash_verification_failure PASSED [ 50%]
tests/test_skill_verification_wiring.py::TestSkillVerificationWiring::test_unapproved_skill_bundle_raises_security_error PASSED [ 60%]
tests/test_session_isolated_snapshots.py::TestSessionIsolatedSnapshots::test_curated_memory_store_get_frozen_turn0_snapshot PASSED [ 70%]
tests/test_session_isolated_snapshots.py::TestSessionIsolatedSnapshots::test_session_scoped_snapshots_isolate_frozen_memory PASSED [ 80%]
tests/test_session_isolated_snapshots.py::TestSessionIsolatedSnapshots::test_soul_enforce_budget_exact_cap_reservation PASSED [ 90%]
tests/test_session_isolated_snapshots.py::TestSessionIsolatedSnapshots::test_soul_provenance_workspace_user_trust_tier PASSED [100%]

============================== 10 passed in 1.39s ==============================
```

### Full Regression Test Suite Execution
```bash
.venv/bin/pytest tests/test_curated_memory.py tests/test_soul_manager.py tests/test_prompt_builder.py tests/test_skill_synthesis.py -v
```

Output:
```
============================== 64 passed in 5.46s ==============================
```

---

## 4. Verification of Must-Haves

| Must-Have Requirement | Status | Evidence |
|-----------------------|--------|----------|
| Skill `verify:` hooks executed in orchestrator task verification | VERIFIED | `_run_skill_verifications()` called in `_execute_standard_dag()` |
| Minimal allowlisted environment (strips provider keys, SSH, credentials) | VERIFIED | `test_minimal_environment_strips_secrets_and_provider_keys` passes |
| Hooks run inside workspace sandbox | VERIFIED | `test_run_skill_verification_in_sandbox` passes |
| `must_pass: true` hook failure blocks `SUCCEEDED` | VERIFIED | `test_orchestrator_gates_task_completion_on_skill_verification` passes |
| Skill bundle validated against SkillSpector SHA-256 hash | VERIFIED | `test_tampered_skill_bundle_triggers_hash_verification_failure` passes |
| Turn-0 memory snapshots session-scoped by `session_id` | VERIFIED | `test_session_scoped_snapshots_isolate_frozen_memory` passes |
| `_enforce_budget` guarantees length `<= MAX_SOUL_CHARS` | VERIFIED | `test_soul_enforce_budget_exact_cap_reservation` passes |
| PromptBuilder recognizes workspace `.hex/SOUL.md` as user-authored | VERIFIED | `test_soul_provenance_workspace_user_trust_tier` passes |
