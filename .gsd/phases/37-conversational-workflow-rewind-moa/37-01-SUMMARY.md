# Phase 37 Plan 01 Summary: Conversational Timeline Rewind & Asynchronous Title Synthesis

## 1. Overview
Phase 37 Plan 01 delivers two core capabilities for conversational workflow supremacy in the HEX Workbench:
1. **Conversational Timeline Rewind Engine (`/rewind <turn_id | -N>`)**:
   Provides historical ledger branching and execution state recovery. Discarded turns are safely archived with `is_active = 0` under unique branch IDs (e.g. `br_rewind_<uuid>`), while corresponding tool calls are marked `REVERTED` and a cryptographically hash-chained `TIMELINE_REWOUND` event is recorded in `task_events`.
2. **Asynchronous Background LLM Session Title Synthesis (`SessionTitleSynthesizer`)**:
   Implements a 2-stage title lifecycle: instant deterministic extraction (Stage 1, `<= 48` chars) on opening prompt submission, followed by a non-blocking background LLM synthesis worker (Stage 2) generating concise 3-5 word session titles updating `tasks.goal` with strict provenance hierarchy (`USER` > `LLM` > `DERIVED`) and anti-chatter / answer-rejection guards. The updated title is immediately queryable and reflected in the Start screen resume list and session switchers.

---

## 2. Tasks Completed

### Task 1: Conversational Timeline Rewind Engine with Ledger Branching
- **Files Modified / Created:**
  - `storage/timeline_rewind.py`: Implemented `RewindTarget`, `RewindResult`, `group_messages_by_turn`, and `TimelineRewindManager`.
  - `storage/db.py`: Added `dialogue_turns` table schema and indexing, implemented `record_dialogue_turn`, `save_dialogue_turns`, `get_active_dialogue`, `get_all_dialogue_turns`, `revert_tool_calls_for_turns`, and `record_timeline_branch`.
  - `storage/__init__.py`: Exported `timeline_rewind` submodule and classes.
  - `agent/react_loop.py`: Added `rewind_to(turn_spec)`.
  - `hex_cli/tui.py`: Added `/rewind` and `/undo` slash commands, `_cmd_rewind`, and updated `/help`.
  - `tests/storage/test_timeline_rewind.py`: Authored 8 unit and integration tests covering relative offsets, absolute turns, boundary clamping, branch ledger integrity, tool call reversal, `ReActLoop`, and `HexConsole`.
- **Commit:** `ac53068 feat(phase-37): implement conversational timeline rewind engine with ledger branching`

### Task 2: Asynchronous Background LLM Session Title Synthesis
- **Files Modified / Created:**
  - `cognition/title_synthesizer.py`: Implemented `TitleProvenance` (USER > LLM > DERIVED), `derive_instant_title`, `validate_synthesized_title` with word count ceiling ($\le 12$ words), CJK character limit ($\le 20$ chars), answer-shaped rejection heuristics, and `SessionTitleSynthesizer` non-blocking worker.
  - `cognition/__init__.py`: Exported `title_synthesizer` submodule and classes.
  - `storage/db.py`: Implemented `update_task_goal` enforcing provenance hierarchy and hash-chained `TASK_TITLE_UPDATED` events.
  - `agent/react_loop.py`: Integrated instant title derivation before Turn 1 and scheduled background title synthesis upon Turn 1 completion.
  - `hex_cli/tui.py`: Wired instant title seeding and background synthesis into `HexConsole`.
  - `tests/cognition/test_title_synthesis.py`: Authored 8 unit and integration tests covering instant derivation, validation guards, provenance hierarchy enforcement, non-blocking execution, and Start screen resume query retrieval.
- **Commit:** `7168da2 feat(phase-37): implement asynchronous background llm session title synthesis`

---

## 3. Verification & Empirical Proof

### Pytest Verification Suite
```bash
.venv/bin/pytest tests/storage/test_timeline_rewind.py tests/cognition/test_title_synthesis.py -v
```
**Output:**
```
============================= test session starts ==============================
platform darwin -- Python 3.14.3, pytest-9.2.0, pluggy-1.7.0 -- /Users/anv./Development/Harness/.venv/bin/python3.14
rootdir: /Users/anv./Development/Harness
configfile: pyproject.toml
plugins: anyio-4.16.0
collecting ... collected 16 items

tests/storage/test_timeline_rewind.py::test_group_messages_by_turn PASSED        [  6%]
tests/storage/test_timeline_rewind.py::test_relative_rewind_single_turn PASSED   [ 12%]
tests/storage/test_timeline_rewind.py::test_relative_rewind_multiple_turns PASSED [ 18%]
tests/storage/test_timeline_rewind.py::test_absolute_rewind PASSED               [ 25%]
tests/storage/test_timeline_rewind.py::test_boundary_clamping PASSED             [ 31%]
tests/storage/test_timeline_rewind.py::test_branch_ledger_integrity_and_tool_reversal PASSED [ 37%]
tests/storage/test_timeline_rewind.py::test_react_loop_rewind_to PASSED          [ 43%]
tests/storage/test_timeline_rewind.py::test_hex_console_rewind_slash_command PASSED [ 50%]
tests/cognition/test_title_synthesis.py::test_derive_instant_title_clean_cases PASSED [ 56%]
tests/cognition/test_title_synthesis.py::test_derive_instant_title_word_boundary_truncation PASSED [ 62%]
tests/cognition/test_title_synthesis.py::test_validate_synthesized_title_guards PASSED [ 68%]
tests/cognition/test_title_synthesis.py::test_provenance_hierarchy_in_ledger PASSED [ 75%]
tests/cognition/test_title_synthesis.py::test_session_title_synthesizer_async PASSED [ 81%]
tests/cognition/test_title_synthesis.py::test_non_blocking_behavior PASSED         [ 87%]
tests/cognition/test_title_synthesis.py::test_react_loop_title_synthesis_integration PASSED [ 93%]
tests/cognition/test_title_synthesis.py::test_start_screen_resume_query_retrieval PASSED [100%]

============================== 16 passed in 1.00s ==============================
```

### Cargo Workspace Test Suite
```bash
cargo test --workspace
```
**Output:**
```
test result: ok. 13 passed; 0 failed; finished in 0.00s (hex_protocol)
test result: ok. 1 passed; 0 failed; finished in 1.07s (live_acp_test)
test result: ok. 3 passed; 0 failed; finished in 0.00s (protocol_test)
test result: ok. 16 passed; 0 failed; finished in 0.25s (hex_tui)
test result: ok. 3 passed; 0 failed; finished in 0.07s (test_context_window)
test result: ok. 8 passed; 0 failed; finished in 0.00s (test_design_system)
test result: ok. 4 passed; 0 failed; finished in 0.09s (test_screens_overlays_and_errors)
test result: ok. 5 passed; 0 failed; finished in 0.10s (test_screens_start_and_session)
test result: ok. 4 passed; 0 failed; finished in 0.20s (test_session_management_and_resume)
test result: ok. 8 passed; 0 failed; finished in 0.30s (test_ui)
test result: ok. 2 passed; 0 failed; finished in 0.10s (test_virtual_render)
test result: ok. 9 passed; 0 failed; finished in 0.17s (ui_rendering_test)
```

---

## 4. Key Architectural Decisions & Safeguards
1. **Zero-Data-Loss Branching**: Rewinding never executes `DELETE FROM dialogue_turns`. Instead, superseded turns receive `is_active = 0` and are associated with a generated `branch_id`, recording a cryptographically hash-chained `TIMELINE_REWOUND` event.
2. **Deterministic Tool Call Invalidation**: Uncommitted and trailing tool calls within rewound turn windows are transitioned to `status = 'REVERTED'`.
3. **Strict Title Provenance Gating**: An explicit user-defined title (`USER`, rank 3) can never be overridden by automated background workers (`LLM`, rank 2) or opening prompt heuristics (`DERIVED`, rank 1).
4. **Anti-Chatter & Answer Rejection**: Background title synthesis strictly rejects conversational openings ("Certainly", "Yes", "Here is"), multi-line answers, prompt echoes, and titles exceeding 12 words or 20 CJK characters.
