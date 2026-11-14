# Phase 29: TUI Ergonomics, Scrolling Fix & ReAct Loop Native Tool-Call Alignment (Fit & Finish Polish) — Verification Report

**Verification Date:** 2026-11-14  
**Status:** FAIL (Gaps Found)  
**Score:** 8/9 Must-Haves Verified  
**Gap Closure Plan:** `.gsd/phases/29-fit-and-finish-polish/29-03-PLAN.md`

---

## 1. Executive Summary

Phase 29 delivered extensive fit & finish polish across two critical architectural pillars:
1. **Plan 29-01 (Rust Terminal Cockpit & Scrolling Ergonomics):** Complete and empirically validated. Ratatui vertical layout in `crates/hex-tui/src/ui/mod.rs` was upgraded to a responsive 4-tier constraint hierarchy (header, dialogue/diff, status bar, input). The dialogue view in `crates/hex-tui/src/ui/dialogue.rs` was upgraded to compute total visual wrapped lines across character widths, eliminating line clipping when pinning to the bottom. Tab focus cycling (`Input -> Dialogue -> Diff`), PageUp/PageDown 5-row jumps, and natural mouse-wheel / input-focus scrolling were implemented. All 39 Rust workspace tests pass, headless virtual verification passes, and all 8 Python launcher integration tests pass.
2. **Plan 29-02 (ReAct Loop Native Tool Schema & Retry Resilience):** Mostly complete, but introduced a regression in downstream multi-turn ReAct consumers. The ReAct loop in `src/harness_core/react_loop.py` successfully emits native `role: "tool"` with `tool_call_id`, `name`, and `content`, while `_call_model` retries transient 429/502/503/504/529 and timeout errors with exponential backoff and jitter. In `src/harness_core/providers/cloud_provider.py`, Anthropic thinking block signatures are preserved and unsigned blocks are safely stripped.
3. **Detected Regression (Gap):** When `self.messages.append` in `ReActLoop.run` was updated to emit native `role: "tool"`, it replaced `"tool_name"` with `"name"` and `"call_id"` with `"tool_call_id"`. Downstream tests and consumers expecting `msg.get("tool_name")` (specifically `tests/test_gateway_process_tools.py:test_react_loop_end_to_end_multi_turn_session`) fail because `tool_names` evaluates to `[None]`, causing the loop to repeatedly invoke `start_process` until max turns are exhausted.
4. **Resolution:** Gap closure plan `29-03-PLAN.md` has been authored to retain `tool_name` and `call_id` as backward-compatible dictionary aliases alongside native `name` and `tool_call_id`.

---

## 2. Must-Haves Verification Matrix

| Must-Have | Status | Evidence |
|:---|:---:|:---|
| **1. Top header and persistent status bar render without layout collision** | **PASS** | `crates/hex-tui/src/ui/mod.rs` (lines 23-65) 4-tier constraint hierarchy; `test_cockpit_full_layout_rendering` passes. |
| **2. Dialogue transcript scrolls naturally via mouse wheel or keys when focus is Focus::Input** | **PASS** | `crates/hex-tui/src/app.rs` lines 699-722 (`scroll_up`/`scroll_down` handle `Focus::Input`); `test_dialogue_scrolling_with_input_focus` passes. |
| **3. Tab key cycles active viewport focus between Input, Dialogue, and Diff panes** | **PASS** | `crates/hex-tui/src/app.rs` line 524 (`switch_focus`, `handle_tab`); `main.rs` line 417; `test_tab_key_focus_cycling` passes. |
| **4. PageUp and PageDown scroll dialogue or diff viewport by 5 rows** | **PASS** | `crates/hex-tui/src/app.rs` lines 724-734 (`handle_page_up`/`handle_page_down`); `test_page_up_page_down_steps` passes. |
| **5. Dialogue max_scroll calculation accounts for visually wrapped lines** | **PASS** | `crates/hex-tui/src/ui/dialogue.rs` lines 356-378 iterates lines computing visual wrapped rows against `area.width`; `test_visual_wrapped_lines_scroll_calculation` passes. |
| **6. ReAct loop tool results emit native role 'tool' with tool_call_id and name for cloud provider compliance** | **FAIL** | Emits `role: "tool"`, `tool_call_id`, `name`, but omits backward-compatible `tool_name` and `call_id` keys, breaking `tests/test_gateway_process_tools.py`. |
| **7. ReAct loop model calls automatically retry up to 3 times on transient 429, 503, or timeout errors with exponential backoff and jitter** | **PASS** | `src/harness_core/react_loop.py` lines 464-575 (`_is_transient_error` + `_call_model`); `test_transient_retry_429_success` passes. |
| **8. Anthropic request builder preserves valid thinking block signatures and safely strips unsigned or corrupted thinking blocks** | **PASS** | `src/harness_core/providers/cloud_provider.py` lines 810-865 and lines 1022-1040; `test_anthropic_thinking_signature_preserved` passes. |
| **9. ReAct resilience test suite passes verifying retry recovery, retry exhaustion, and thinking block preservation** | **PASS** | `tests/test_react_loop_resilience.py` (9/9 passing) and `tests/test_react_loop.py` (31/31 passing). |

---

## 3. Detailed Empirical Evidence

### 3.1 Cargo Workspace Test Suite (`cargo test --workspace`)
```text
running 13 tests (hex-protocol unit) ... ok
running 1 test (live_acp_test) ... ok
running 3 tests (protocol_test) ... ok
running 6 tests (hex-tui unit) ... ok
running 5 tests (crates/hex-tui/tests/test_ui.rs) ... ok
running 11 tests (crates/hex-tui/tests/ui_rendering_test.rs) ... ok
Total: 39 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.06s
```

### 3.2 Headless Virtual TUI Frame Verification (`cargo run -p hex-tui -- --check`)
```text
HEX TUI headless verification passed: 1 virtual frame rendered successfully.
Exit status: 0
```

### 3.3 Python Launcher Integration Tests (`.venv/bin/python -m unittest tests/test_rust_tui_launcher.py`)
```text
Ran 8 tests in 0.121s
OK
```

### 3.4 ReAct Loop Resilience Tests (`.venv/bin/python -m unittest tests/test_react_loop_resilience.py`)
```text
Ran 9 tests in 0.019s
OK
```
Tests verified:
- `test_tool_result_native_format`: Verified native `role: "tool"`, `tool_call_id`, and `name`.
- `test_transient_retry_429_success`: Verified 429 retried twice and succeeded on attempt 3.
- `test_transient_retry_503_and_timeout`: Verified 503 and timeout recovery.
- `test_retry_exhaustion_reports_error`: Verified perpetual 429 terminates with `termination_reason="error"`.
- `test_non_transient_no_retry`: Verified HTTP 400 and `ValueError` do not retry.
- `test_anthropic_thinking_signature_preserved`: Verified valid thinking blocks preserved.
- `test_anthropic_thinking_signature_suppressed_when_missing`: Verified unsigned thinking blocks stripped.
- `test_anthropic_tool_message_translation`: Verified `role: "tool"` converted to `tool_result` block.
- `test_anthropic_parse_response_retains_thinking_signature`: Verified response thinking blocks indexed.

### 3.5 ReAct Loop Core Suite (`.venv/bin/python -m unittest tests/test_react_loop.py`)
```text
Ran 31 tests in 2.517s
OK
```

### 3.6 Downstream Regression Detection (`.venv/bin/python -m unittest tests/test_gateway_process_tools.py`)
```text
FAIL: test_react_loop_end_to_end_multi_turn_session (tests.test_gateway_process_tools.TestGatewayProcessTools.test_react_loop_end_to_end_multi_turn_session)
Test full multi-turn autonomous ReAct loop session managing background process lifecycle.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/Users/anv./Development/Harness/tests/test_gateway_process_tools.py", line 305, in test_react_loop_end_to_end_multi_turn_session
    self.assertTrue(res.success)
AssertionError: False is not true

Ran 11 tests in 6.871s
FAILED (failures=1)
```

**Root Cause:**
In `src/harness_core/react_loop.py` lines 972-980:
```python
self.messages.append(
    {
        "role": "tool",
        "tool_call_id": tool_res.call_id,
        "name": tool_res.name,
        "content": obs_payload,
        "observation": obs_content,
    }
)
```
Omitting `tool_name` causes `tool_names = [m.get("tool_name") for m in messages if isinstance(m, dict)]` in `test_gateway_process_tools.py` line 269 to return `[None]`, getting trapped in an infinite retry of the first turn until `max_turns` is reached.

---

## 4. Benchmark Parity Assessment (`benchmarks/hermes-agent/`)

| Capability | Hermes Implementation | HEX Implementation | Parity Status |
|:---|:---|:---|:---|
| **Tool Message Schema** | Emits `role: "tool"` with `tool_call_id`, `name`, and `content` (`benchmarks/hermes-agent/run_agent.py` line 1125). | Emits `role: "tool"` with `tool_call_id`, `name`, `content`, and backward-compatible `observation` XML tag. | **Meets Parity** (Requires adding legacy aliases `tool_name` and `call_id` to prevent breaking existing callers). |
| **Retry Backoff Mechanics** | Jittered exponential backoff: `delay = min(base * 2^(attempt-1), max_delay) + uniform_jitter` (`benchmarks/hermes-agent/agent/retry_utils.py` line 121). | Jittered exponential backoff: `delay = initial_delay * (backoff_factor ** attempt) + random.uniform(0, jitter)` (`src/harness_core/react_loop.py` line 572). Configurable via `ReActConfig`. | **Meets Parity** |
| **Frontier Thinking Protection** | Strips thinking signatures on third-party proxies (`anthropic_adapter.py` line 662). | Retains valid thinking signatures; suppresses unsigned thinking blocks to prevent Anthropic HTTP 400 (`cloud_provider.py` lines 810-865). | **Exceeds Parity** |

---

## 5. Gap Closure Plan

The failure has been isolated and packaged into:
- **Plan File:** `.gsd/phases/29-fit-and-finish-polish/29-03-PLAN.md` (`gap_closure: true`)
- **Required Action:**
  1. Add `"tool_name": tool_res.name` and `"call_id": tool_res.call_id` to `self.messages.append` in `src/harness_core/react_loop.py`.
  2. Update `tests/test_gateway_process_tools.py` line 269 to inspect `m.get("tool_name") or m.get("name")`.
  3. Re-run `.venv/bin/python -m unittest tests/test_gateway_process_tools.py tests/test_react_loop.py tests/test_react_loop_resilience.py`.
