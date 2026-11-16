# Phase 37 Plan 02 Summary: Mixture-of-Agents Architecture & Repetition Sentry

## 1. Overview
Phase 37 Plan 02 delivers two high-impact cognitive systems for reasoning and execution supremacy in the HEX Workbench:
1. **Mixture-of-Agents (MoA) Multi-Model Synthesis Engine (`src/harness_core/core/moa.py`)**:
   Coordinates concurrent reasoning queries across multiple candidate proposer models in parallel, handles timeouts and partial proposer failures gracefully, synthesizes cohesive solutions via a primary aggregator model, attributes per-model token consumption and latency metrics, and falls back directly to the aggregator if proposers fail. Deeply integrated into `ReActLoop` and `HarnessOrchestrator` with hash-chained ledger event journaling (`MOA_SYNTHESIS_COMPLETED`).
2. **Degenerate Loop / Repetition Sentry & Thinking Tag Scrubber (`src/harness_core/cognition/repetition_sentry.py`)**:
   Intercepts model token repetition loops using fast line-echo, 60-character sliding-window, and periodic anchor scans while protecting legitimate tabular/batch multiline structures. Detects tool call thrashing (repeated identical calls $\ge 3$ times with error/empty output) and cyclical ping-pong action loops (e.g. `A -> B -> A -> B`), halts redundant executions, and injects authoritative out-of-band system steering prompts (`[SYSTEM STEERING: ...]`). Scrubs thinking tags (`<think>...</think>`, `<thought>`, `<reasoning>`, and unclosed streaming tails) to prevent reasoning leakage into dialogue history or storage.

---

## 2. Tasks Completed

### Task 1: Mixture-of-Agents (MoA) Proposer-Aggregator Engine
- **Files Modified / Created:**
  - `src/harness_core/core/moa.py`: Implemented `ProposerConfig`, `MoAConfig`, `ProposerOutput`, `MoAResult`, and `MixtureOfAgents` with `query_proposers`, `build_aggregator_prompt`, and `synthesize`.
  - `src/harness_core/core/__init__.py`: Exported MoA classes and configuration models via lazy loader and `__all__`.
  - `src/harness_core/core/orchestrator.py`: Wired `moa_config` through `HarnessOrchestrator.__init__` and `run_agent_loop`.
  - `src/harness_core/core/react_loop.py`: Added `moa_config` to `ReActConfig`, attached `moa_result` to `ReActTurn`, routed reasoning turns through `MixtureOfAgents.synthesize`, recorded `_record_moa_in_ledger` with `MOA_SYNTHESIS_COMPLETED` hash-chained ledger events, and enabled dictionary subscripting / contains on `ReActTurn` and `MoAResult`.
  - `tests/test_mixture_of_agents.py`: Authored 7 comprehensive unit and integration tests covering parallel execution, partial proposer resilience, timeout isolation, fallback when all proposers fail, aggregator prompt compilation, ReActLoop integration with ledger validation, and orchestrator wiring.
- **Commit:** `8aa5b65 feat(phase-37): implement mixture-of-agents proposer-aggregator engine`

### Task 2: Degenerate Loop / Repetition Sentry & Thinking Tag Scrubber
- **Files Modified / Created:**
  - `src/harness_core/cognition/repetition_sentry.py`: Implemented `is_repetition_dominated`, `is_runaway_repetition` (with `_line_repetition_dominated`, `_window_count_dominated`, `_periodic_run_dominated`, and distinct non-empty line ratio gating), `ActionRecord`, `ActionLoopDetector` (tracking identical call repeats $\ge 3$ and ping-pong cycles $\ge 2$), `generate_steering_prompt`, `strip_thinking_tags` (closed tags and unclosed streaming tails), and `RepetitionSentry`.
  - `src/harness_core/cognition/__init__.py`: Exported `RepetitionSentry`, `ActionLoopDetector`, `strip_thinking_tags`, and related symbols in lazy loader and `__all__`.
  - `src/harness_core/core/react_loop.py`: Wired `is_runaway_repetition` and token repetition steering into `_invoke_caller`, integrated `ActionLoopDetector` into tool invocation cycles to intercept thrashing and inject out-of-band steering prompts without redundant execution, and cleaned assistant outputs with `strip_thinking_tags` before persisting to `messages` and `turns`.
  - `src/harness_core/client/tui.py`: Filtered assistant outputs and ReAct results with `strip_thinking_tags` before console display and message history persistence.
  - `tests/test_repetition_sentry.py`: Authored 16 unit and integration tests covering verbatim line repeats, window dominance, legitimate batch inserts and markdown tables without false positives, action thrashing, ping-pong cycle detection, authoritative steering prompts, closed and unclosed thinking tag extraction, and ReActLoop integration.
- **Commit:** `32a8da2 feat(phase-37): implement repetition sentry and thinking tag scrubber`

---

## 3. Verification & Empirical Proof

### Pytest Verification Suite
```bash
.venv/bin/pytest tests/test_mixture_of_agents.py tests/test_repetition_sentry.py -v
```
**Output:**
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.2.0, pluggy-1.7.0 -- /Users/anv./Development/Harness/.venv/bin/python3.14
cachedir: .pytest_cache
rootdir: /Users/anv./Development/Harness
configfile: pyproject.toml
plugins: anyio-4.16.0
collecting ... collected 23 items

tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_parallel_proposer_execution_and_synthesis PASSED [  4%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_partial_proposer_failure_resilience PASSED [  8%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_timeout_proposer_handled_gracefully PASSED [ 13%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_fallback_when_all_proposers_fail PASSED [ 17%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_build_aggregator_prompt_structure PASSED [ 21%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_react_loop_with_moa_integration PASSED [ 26%]
tests/test_mixture_of_agents.py::TestMixtureOfAgents::test_orchestrator_run_agent_loop_wires_moa PASSED [ 30%]
tests/test_repetition_sentry.py::TestRepetitionSentry::test_line_repetition_dominated PASSED [ 34%]
tests/test_repetition_sentry.py::TestRepetitionSentry::test_window_repetition_without_newlines PASSED [ 39%]
tests/test_repetition_sentry.py::TestRepetitionSentry::test_legitimate_batch_inserts_not_flagged PASSED [ 43%]
tests/test_repetition_sentry.py::TestRepetitionSentry::test_legitimate_markdown_table_not_flagged PASSED [ 47%]
tests/test_repetition_sentry.py::TestRepetitionSentry::test_short_fragments_fail_open PASSED [ 52%]
tests/test_repetition_sentry.py::TestActionLoopDetector::test_identical_call_repetition_detected PASSED [ 56%]
tests/test_repetition_sentry.py::TestActionLoopDetector::test_successful_calls_do_not_trip_identical_repetition PASSED [ 60%]
tests/test_repetition_sentry.py::TestActionLoopDetector::test_ping_pong_action_cycle_detected PASSED [ 65%]
tests/test_repetition_sentry.py::TestActionLoopDetector::test_generate_steering_prompt_authoritative PASSED [ 69%]
tests/test_repetition_sentry.py::TestThinkingTagScrubber::test_closed_thinking_tags_scrubbed PASSED [ 73%]
tests/test_repetition_sentry.py::TestThinkingTagScrubber::test_case_insensitive_and_alternate_tags PASSED [ 78%]
tests/test_repetition_sentry.py::TestThinkingTagScrubber::test_unclosed_streaming_tail_scrubbed PASSED [ 82%]
tests/test_repetition_sentry.py::TestThinkingTagScrubber::test_text_without_thinking_tags_untouched PASSED [ 86%]
tests/test_repetition_sentry.py::TestReActLoopRepetitionIntegration::test_react_loop_intercepts_runaway_token_repetition PASSED [ 91%]
tests/test_repetition_sentry.py::TestReActLoopRepetitionIntegration::test_react_loop_intercepts_action_thrashing_with_steering PASSED [ 95%]
tests/test_repetition_sentry.py::TestReActLoopRepetitionIntegration::test_react_loop_scrubs_thinking_tags_from_history PASSED [100%]

============================== 23 passed in 1.97s ==============================
```

### Cargo Workspace Test Suite
```bash
cargo test --workspace
```
**Output:**
```
test result: ok. 13 passed; 0 failed; finished in 0.00s (hex_protocol)
test result: ok. 1 passed; 0 failed; finished in 1.10s (live_acp_test)
test result: ok. 3 passed; 0 failed; finished in 0.00s (protocol_test)
test result: ok. 16 passed; 0 failed; finished in 0.26s (hex_tui)
test result: ok. 3 passed; 0 failed; finished in 0.07s (test_context_window)
test result: ok. 8 passed; 0 failed; finished in 0.00s (test_design_system)
test result: ok. 4 passed; 0 failed; finished in 0.09s (test_screens_overlays_and_errors)
test result: ok. 5 passed; 0 failed; finished in 0.11s (test_screens_start_and_session)
test result: ok. 4 passed; 0 failed; finished in 0.22s (test_session_management_and_resume)
test result: ok. 8 passed; 0 failed; finished in 0.29s (test_ui)
test result: ok. 2 passed; 0 failed; finished in 0.10s (test_virtual_render)
test result: ok. 9 passed; 0 failed; finished in 0.16s (ui_rendering_test)
```

---

## 4. Key Architectural Decisions & Safeguards
1. **Concurrency Isolation & Partial Failure Resilience**: Proposer models fan out concurrently under a bounded thread pool with individual timeout deadlines. As long as `min_proposers_required` succeeds, the aggregator produces a synthesized answer without failing the user turn. If all proposers fail, MoA falls back directly to the aggregator.
2. **Accurate Token & Latency Attribution**: `ProposerOutput` and `MoAResult` measure and aggregate per-candidate latencies and token usage, persisting metrics into `task_events` with the `MOA_SYNTHESIS_COMPLETED` hash-chained event.
3. **Distinct Line Ratio Defense**: Batch database outputs and markdown tables share long prefixes and keywords but have distinct line contents; `is_runaway_repetition` measures distinct non-empty line ratio ($\le 0.5$) to eliminate false positives on legitimate bulk output.
4. **Action Thrashing Short-Circuit**: When identical failing tool calls ($\ge 3$) or cyclical ping-pong patterns ($\ge 2$ periods) are detected, real tool execution is skipped, saving host resources, and authoritative steering prompts are injected directly into the agent's observation channel.
5. **Thinking Tag Zero-Leakage Guarantee**: Model internal scratchpad blocks (`<think>...</think>`, `<thought>`, `<reasoning>`, `<REASONING_SCRATCHPAD>`) and streaming open tails are extracted and scrubbed, guaranteeing internal thoughts are cleanly segregated from user dialogues and stored deliverables.
