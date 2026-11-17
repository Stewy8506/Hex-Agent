# Phase 29 Plan 02: ReAct Loop Native Tool Schema & Transient Retry Resilience Summary

**Execution Date:** 2026-11-14  
**Status:** COMPLETE (2/2 Tasks Done, 100% Empirical Verification)  
**Commits:**
- `1bf15f9`: `feat(phase-29): native tool result schema and transient retry backoff`
- `6c167df`: `feat(phase-29): anthropic thinking block signature protection and resilience test suite`

---

## 1. Accomplishments

### Task 1: Native Tool Schema History Formatting & Transient Error Retry Resilience
- **Native Tool Role Schema in ReAct Loop (`harness_core/react_loop.py`):**
  - Updated tool execution output appending in `ReActLoop.run` so tool observations emit standard `role: "tool"` schemas with `tool_call_id`, `name`, and unescaped `content` (`obs_payload`), while preserving `observation` XML tags (`obs_content`) for backward compatibility with existing tests and prompting engines.
  - Aligned message serialization with OpenAI/Anthropic/Gemini multi-turn tool calling specifications where each tool result references the preceding assistant's `tool_call_id`.
  - Updated `_format_messages_to_prompt` to format `role == "TOOL"` into standard plaintext prompt blocks `[TOOL ({tool_name})]\n{content}`.
  - Updated `HarnessOrchestrator.run_agent_loop` deterministic tool planner fallback to inspect `role in ("user", "observation", "tool")` and read `m.get("observation") or m.get("content")`.
- **Transient Error Retry Resilience (`harness_core/react_loop.py`):**
  - Added configurable retry parameters to `ReActConfig`: `max_retries: int = 3`, `initial_retry_delay: float = 0.5`, `backoff_factor: float = 2.0`, and `retry_jitter: float = 0.1`.
  - Implemented `_is_transient_error(exc: Exception) -> bool` classifying HTTP 429 (rate limit), 503 (service unavailable / overloaded), 502, 504, 529, timeouts (`TimeoutError`, `socket.timeout`), and connection errors as transient, while immediately rejecting non-transient errors (HTTP 400, 401, 403, 404, `ValueError`, `TypeError`, `BudgetExceededError`, `PolicyBlockedError`).
  - Implemented iterative retry loop in `_call_model` with exponential backoff and randomized jitter, tracking `retry_count` on `ReActLoop` and `ReActSessionResult`.
- **Verification:**
  - Ran `tests/core/test_react_loop.py` (31/31 unit and integration tests passing).

### Task 2: Anthropic Thinking Block Signature Preservation & Comprehensive Resilience Tests
- **Anthropic Thinking Block Protection & Tool Translation (`providers/cloud_provider.py`):**
  - In `_parse_response`, detected content blocks with `item.get("type") == "thinking"` and indexed them under `data["thinking_blocks"]`, `data["thinking"]`, and `data["signature"]` so multi-turn loops retain valid thinking signatures.
  - In `_build_request`, translated `role: "tool"` messages to Anthropic's native `tool_result` content block inside a `role: "user"` message (`{"role": "user", "content": [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": str(msg["content"])}]}`).
  - Hardened assistant thinking block serialization:
    - If a thinking block contains a valid, non-empty `signature`, preserved the exact structure `{"type": "thinking", "thinking": "...", "signature": signature}`.
    - If a thinking block lacks a signature or contains an empty signature, safely stripped/suppressed the thinking block from the payload sent to Anthropic, avoiding HTTP 400 `Invalid signature for thinking block` crashes.
  - Added native multi-turn `messages` argument handling to `CloudModelProvider.generate()` and `_build_request()` across Anthropic, OpenAI, and Gemini backends.
- **Resilience Test Suite (`tests/core/test_react_loop_resilience.py`):**
  - `test_tool_result_native_format`: Verified tool results emit `role: "tool"`, `tool_call_id`, and `name`.
  - `test_transient_retry_429_success`: Verified 429 retries twice, succeeds on attempt 3 with `retry_count == 2`.
  - `test_transient_retry_503_and_timeout`: Verified 503 and `TimeoutError` retries with clean recovery.
  - `test_retry_exhaustion_reports_error`: Verified perpetual 429 exhausts retries, raises exception, and terminates with `termination_reason="error"`, `success=False`.
  - `test_non_transient_no_retry`: Verified `ValueError` and HTTP 400 fail immediately on attempt 1 without retries.
  - `test_anthropic_thinking_signature_preserved`: Verified `_build_request` preserves valid thinking blocks and signatures.
  - `test_anthropic_thinking_signature_suppressed_when_missing`: Verified `_build_request` strips unsigned/empty thinking blocks.
  - `test_anthropic_tool_message_translation`: Verified `role: "tool"` translates into Anthropic `tool_result` block.
  - `test_anthropic_parse_response_retains_thinking_signature`: Verified `_parse_response` indexes thinking blocks and signatures in `raw`.
- **Verification:**
  - Ran `tests/core/test_react_loop_resilience.py` (9/9 tests passing).
  - Ran `tests/core/test_react_loop.py` (31/31 tests passing).
  - Ran `tests/providers/test_model_providers.py`, `tests/providers/test_extended_cloud_providers.py`, `tests/integration/test_two_tier_compression_benchmark.py` (36/36 tests passing).

---

## 2. Deviations

1. **Deterministic tool planner in `orchestrator.py`:**
   - *Deviation:* Updated `_react_model_caller` in `harness_core/orchestrator.py` to check `m.get("role") in ("user", "observation", "tool")` and read `m.get("observation") or m.get("content")`.
   - *Justification:* When ReAct loop emitted native `role: "tool"`, deterministic tool execution tests in `test_react_loop.py` (`test_run_agent_loop_deterministic_tool_execution` and `test_run_agent_loop_custom_workspace`) required seeing the observation in history to trigger completion.

---

## 3. Verification Artifacts

- Test suite: `tests/core/test_react_loop_resilience.py` (9 tests, 0 failures)
- Test suite: `tests/core/test_react_loop.py` (31 tests, 0 failures)
- Full regression check: 76 tests across ReAct loop, model providers, and resilience passing cleanly.
