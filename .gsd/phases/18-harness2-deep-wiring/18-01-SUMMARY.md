# Phase 18 Plan 01 Summary: Google Research Harness² Experience Library Integration

## Executive Overview
Successfully integrated Google Research Harness²'s per-domain Experience Library (`harness2.core.library.Library`) directly into `HarnessOrchestrator` (`src/harness_core/orchestrator.py`). The orchestrator now lazily instantiates and caches domain-scoped `Library` instances under a configurable library root (`data_dir / "harness2_library"` by default), automatically validates and records post-task experience (`cue`, `lesson`, and `evidence`) upon task success, materializes the adopted component tree (`OPENCODE_SURFACE`), generates canonical `EXPERIENCE.md`, rebuilds `INDEX.md`, logs hash-chained `HARNESS2_EXPERIENCE_RECORDED` ledger events, and retrieves proven domain lessons to inject into autonomous repair and planning prompts.

## Executed Tasks

### Task 1: Wire harness2.core.library.Library Engine and Post-Task Experience Recording into HarnessOrchestrator
- **Files Modified:** `src/harness_core/orchestrator.py`
- **Commit:** `3207cbe` (`feat(phase-18): wire harness2 library and experience recording into orchestrator`)
- **Key Deliverables:**
  - Resolved Google Research Harness² vendor path (`VENDOR_HARNESS2 = get_harness2_dir()`) and imported `Library`, `domain_dir`, `has_experience`, and `OPENCODE_SURFACE`.
  - Added `library_root: Path | str | None = None` parameter to `HarnessOrchestrator.__init__`, defaulting to `self.data_dir / "harness2_library"` with auto-creation.
  - Implemented `get_domain_library(domain)` with domain name normalization and instance caching in `self._domain_libraries`.
  - Implemented `record_task_experience()` validating non-empty `cue`, `lesson`, and `evidence` strings, auto-assembling active harness trees (`systemprompt.md`, `guardrails.md`, `LongTermMEMORY.md`, `tool_descriptions/tool_guidance.md`, plus scanned `skills/*/SKILL.md`), and delegating to `Library.add()`.
  - Wired post-task experience recording into `_execute_standard_dag()` on task success, logging hash-chained `HARNESS2_EXPERIENCE_RECORDED` ledger events.

### Task 2: Implement Pre-Task Domain Experience Retrieval and Context Injection into Planning and Repair Loops
- **Files Modified:** `src/harness_core/orchestrator.py`
- **Commit:** `0bea440` (`feat(phase-18): implement pre-task experience retrieval and repair context injection`)
- **Key Deliverables:**
  - Implemented `retrieve_domain_experience(domain, max_entries=5)` reading `INDEX.md` and parsing `EXPERIENCE.md` files to extract cues, lessons, and evidence, formatting markdown context for model reasoning.
  - Implemented `get_domain_experience_summary(domain)` helper returning `total_entries`, `entries`, `index_path`, and `has_context` status for CLI inspection and diagnostics.
  - Wired domain lessons retrieval into `generate_repair_patch()`, injecting proven domain lessons into `cacheable_context_prefix` alongside architecture summaries and AST blast radius reports.

### Task 3: Author Comprehensive Unit and Integration Test Suite for Experience Library Wiring
- **Files Modified:** `tests/test_harness2_library_wiring.py`
- **Commit:** `cf27fbc` (`test(phase-18): add comprehensive unit and integration test suite for harness2 library wiring`)
- **Key Deliverables:**
  - Authored comprehensive test suite in `tests/test_harness2_library_wiring.py` with 5 rigorous test cases:
    1. `test_library_initialization_defaults_and_custom_root`: Verifies default and custom library roots, domain normalization, and instance caching.
    2. `test_record_task_experience_validation_and_persistence`: Tests rejection of incomplete experience dicts, successful writing of `EXPERIENCE.md`, adopted harness tree, and `INDEX.md` generation.
    3. `test_pre_task_experience_retrieval`: Proves empty domain behavior and multi-entry retrieval with `max_entries` clamping.
    4. `test_multi_domain_isolation`: Confirms strict isolation between independent domains (`security` vs `ui_components`) with zero cross-contamination.
    5. `test_end_to_end_task_completion_records_experience_and_informs_repair`: Validates standard DAG execution writing experience to disk and ledger, followed by prompt context injection in a subsequent repair attempt.

## Empirical Verification
- **Task 1 Verification:**
  ```bash
  .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator(':memory:'); lib = orch.get_domain_library('backend'); print('Library root exists:', orch.library_root.is_dir(), 'domain dir:', lib.dir.name)"
  # Output: Library root exists: True domain dir: backend
  ```
- **Task 2 Verification:**
  ```bash
  .venv/bin/python -c "from harness_core.orchestrator import HarnessOrchestrator; orch = HarnessOrchestrator(':memory:'); print('Domain summary:', orch.get_domain_experience_summary('general'))"
  # Output: Domain summary: {'domain': 'general', 'total_entries': 0, 'entries': [], 'index_path': '/private/.../harness2_library/general/INDEX.md', 'has_context': False}
  ```
- **Task 3 Verification:**
  ```bash
  .venv/bin/python -m unittest tests/test_harness2_library_wiring.py -v
  # Output: Ran 5 tests in 3.135s - OK (5/5 green)
  ```

## Deviations
None. All components were implemented according to the plan specifications with zero external dependencies and zero regressions.
