# Plan 26-01 Summary: Stale Tool Demotion & Atomic Tool-Boundary Preservation

## Implementation Summary

In Plan 26-01, we implemented Phases 1 and 2 of the 4-Phase Context Compactor in `src/harness_core/context_compressor.py` matching Hermes Agent (`agent/context_compressor.py`, `agent/context_compressor_tail.py`, and `agent/compression_marker.py`), accompanied by an extensive test suite in `tests/test_context_compressor_tail_pruning.py`.

### 1. Token Estimation & Elision Markers (Task 1)
- Implemented `estimate_tokens_rough`: ceiling-based character-to-token heuristic (~4 chars/token, minimum 1 token for non-empty text, 0 for empty/None).
- Implemented `estimate_message_tokens`: accounts for envelope framing (~4 tokens), role overhead, string and multimodal content blocks (including image token estimation), and serialized tool call structures.
- Implemented `estimate_messages_tokens`: estimates aggregate token counts across full multi-turn dialog lists.
- Implemented un-imitable elision markers (`HEX_COMPRESSION_MARKER_PREFIX = "⟪HEX-CONTEXT-COMPRESSION:"`) and utilities `elide` and `elide_middle`, computing exact omitted character counts and line counts.

### 2. Stale Tool Result Demotion (Task 1)
- Implemented `demote_stale_tail_tools`:
  - Parameterized with `keep_tool_rounds` (default 4) and `min_demote_chars` (default 1000).
  - Scans from the tail backward to protect the newest tool execution rounds, ensuring the active ReAct tool loop is never broken.
  - Automatically handles both structured format (`role: "tool"`) and XML format (`role: "user"` with `<observation tool="...">`).
  - Replaces verbose tool outputs with structured recovery stubs retaining head and tail chunks, character counts, and omitted line metrics.
  - Strictly preserves tool error outputs (`is_error=True`, `Error:`, `Traceback`), ensuring failed tool invocations remain visible for repair decisions.

### 3. Atomic Tool-Pair Boundary Protection & Alignment (Task 2)
- Implemented `find_tool_call_groups`: identifies atomic call-and-response clusters across structured JSON tool calls and XML `<tool_call>` / `<observation>` turns, accurately grouping parallel tool call batches.
- Implemented `align_boundary_forward`: advances a compaction cut index past the end of any split tool group or orphan tool results (`end_idx + 1`), ensuring tool results are not severed from their assistant call.
- Implemented `align_boundary_backward`: retreats a compaction cut index to the start of an assistant tool call (`start_idx`), ensuring both call and results stay in the subsequent segment.
- Implemented `validate_atomic_boundaries`: validates message sequences for orphan tool results, severed tool calls, and mismatched IDs, preventing provider HTTP 400 Bad Request errors.

### 4. Empirical Test Verification (Task 3)
- Created comprehensive test suite in `tests/test_context_compressor_tail_pruning.py` with 8 test cases verifying token estimation, elision markers, structured and XML tail demotion, error preservation, forward/backward boundary alignment, and boundary validation.

---

## Verification Evidence

1. **Phase 1 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.context_compressor import estimate_tokens_rough, elide_middle, demote_stale_tail_tools; msgs = [{'role': 'tool', 'content': 'x' * 2000, 'name': 'read_file'} for _ in range(8)]; res, count = demote_stale_tail_tools(msgs, keep_tool_rounds=4); assert count == 4, f'Expected 4 demoted, got {count}'; print('Phase 1 Stale Tool Demotion verified:', count, 'demoted')"
   # Output: Phase 1 Stale Tool Demotion verified: 4 demoted
   ```

2. **Phase 2 Verification:**
   ```bash
   .venv/bin/python -c "from harness_core.context_compressor import align_boundary_forward, align_boundary_backward, validate_atomic_boundaries; msgs = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'test'}}]}, {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'ok'}, {'role': 'user', 'content': 'next'}]; assert align_boundary_forward(msgs, 2) == 3; assert align_boundary_backward(msgs, 2) == 1; print('Phase 2 Atomic Alignment verified')"
   # Output: Phase 2 Atomic Alignment verified
   ```

3. **Phase 3 Test Suite:**
   ```bash
   .venv/bin/python -m unittest tests/test_context_compressor_tail_pruning.py
   # Output:
   # ........
   # ----------------------------------------------------------------------
   # Ran 8 tests in 0.001s
   # OK
   ```

---

## Git Commits
- `18c3bfb`: `feat(phase-26): token estimation elision markers and stale tool demotion`
- `b8529ef`: `feat(phase-26): atomic tool-pair boundary protection and alignment`
- `1266b3c`: `feat(phase-26): unit test suite for tail pruning and boundary preservation`
