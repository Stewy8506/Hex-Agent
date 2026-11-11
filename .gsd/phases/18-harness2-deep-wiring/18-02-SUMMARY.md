# Plan 18-02: OrchestratorBenchmarkAdapter & Propose-Probe-Compose Rollout Engine — Summary

## 1. Overview & Objective
Plan 18-02 delivered deep wiring between HEX's autonomous `HarnessOrchestrator` engine and Google Research Harness²'s `BenchmarkAdapter` protocol (`harness2.core.spec`). Real autonomous agent rollouts now execute within isolated workspace sandboxes, format execution traces in standard `[ACTION]/[OBSERVATION]/[ERROR]` grammar, and participate in multi-rollout candidate sweeps (parallel and sequential) managed by Google Research's `harness2.core.recursion` loop.

## 2. Completed Tasks

### Task 1: Implement OrchestratorBenchmarkAdapter Conforming to harness2.core.spec.BenchmarkAdapter Protocol
- **Implementation**: Created `src/harness_core/harness2_adapter.py` providing `OrchestratorBenchmarkAdapter` satisfying all requirements of `harness2.core.spec.BenchmarkAdapter`.
- **Key Features**:
  - Base harness initialization under `.harness/` with required `opencode` surface components (`systemprompt.md`, `guardrails.md`, `LongTermMEMORY.md`, `tool_descriptions/tool_guidance.md`).
  - Task specification and dynamic registration methods (`tasks`, `domain_of`, `task_prompt`, `task_files`, `validate_tree`).
  - Isolated execution environment (`runs_root/executions/<run_id>`) with dedicated per-rollout `workspace/`.
  - Autonomous task submission to `HarnessOrchestrator.submit_and_run(..., execution_mode="standard")`.
  - Tool call extraction and translation into standard `[ACTION] ... \n[OBSERVATION] ... \n` (or `[ERROR] ...`) trajectory logs.
  - Deliverable verification, collection, extraction (`delivered`, `trajectory`, `deliverable_text`, `deliverable_files`), and scoring (`score`).
  - Re-exported `OrchestratorBenchmarkAdapter` in `src/harness_core/proof.py`.
- **Commit**: `5f1a59f`

### Task 2: Connect Recursion Rollout Sweeps (run_parallel & run_sequential) and Wire hex harness2-eval CLI Subcommand
- **Implementation**: Implemented `LocalRouterProposer` and `run_orchestrator_sweep` in `src/harness_core/harness2_adapter.py`.
- **Key Features**:
  - `run_orchestrator_sweep` executes base rollouts and wires them into `harness2.core.recursion.run_parallel` (width-based candidate aggregation) and `harness2.core.recursion.run_sequential` (depth-based candidate iteration).
  - Built `LocalRouterProposer` for deterministic local candidate refinements (`<notes>...</notes>`), candidate selections (`<why>...</why>\n```choice\n...```), and experience synthesis (`<edits>...</edits>\n<experience>...</experience>`).
  - Registered `hex harness2-eval` CLI subcommand in `src/harness_core/cli.py` supporting `--task-id`, `--mode` (parallel / sequential), `--k`, `--runs-root`, and `--json`.
  - Added clean human-readable summary table and raw JSON output options with 0 regressions to `hex harness2-demo`.
- **Commit**: `7c652a3`

### Task 3: Author Comprehensive Unit and Integration Test Suite for Benchmark Adapter and Sweep Runner
- **Implementation**: Authored `tests/test_harness2_benchmark_adapter.py` covering:
  - Protocol conformance: `issubclass` and `isinstance` checks against `BenchmarkAdapter`, and presence of all 12+ required methods and attributes.
  - Rollout execution: isolated workspace directories, trajectory markdown generation in `[ACTION]/[OBSERVATION]` grammar, deliverable validation, observation truncation, and scoring.
  - Multi-rollout sweeps: live parallel (`mode="parallel"`) and sequential (`mode="sequential"`) sweeps verifying `ModeResult.ok`, finals, stages, and serialization.
  - CLI execution: `hex harness2-eval --help`, `--json` output, and formatted summary table.
  - Regression safety: verifying proof re-exports and continuous pass rate across existing test suites (`test_harness_stages.py`, `test_harness2_library_wiring.py`).
- **Commit**: `b331811`

## 3. Verification & Metrics
- All 12 unit and integration tests in `tests/test_harness2_benchmark_adapter.py` passed green (100%).
- Full suite of 27 related tests in `tests/test_harness2_benchmark_adapter.py`, `tests/test_harness_stages.py`, and `tests/test_harness2_library_wiring.py` passed cleanly in 13.98s with 0 errors.
- CLI verification:
  - `python -m harness_core.cli harness2-eval --help` (exit code 0).
  - `python -m harness_core.cli harness2-eval --task-id ledger-1 --k 1 --mode parallel` (exit code 0, Status: SUCCESS, Winning Finals: agg1).
  - `python -m harness_core.cli harness2-eval --task-id ledger-1 --k 1 --json` (valid JSON payload).
