# Phase 26 Plan 02: Middle-Turn Summarizer & Immutable Prompt-Cache Prefixing Summary

**Phase:** 26 (4-Phase Context Compactor & Prompt Cache Locking)  
**Plan:** 26-02  
**Status:** Complete  
**Commits:** `c08d92c`, `24ecbfa`, `27d1a47`  

---

## 1. Executive Summary

Plan 26-02 completed the remaining two phases of the 4-Phase Context Compactor and delivered full end-to-end integration across the HEX workbench runtime (`ReActLoop`, `TwoTierModelRouter`, `HarnessOrchestrator`, and `HexConsole` TUI):

1. **Phase 3 Middle-Turn Summarization & Core Engine (`ContextCompactor`):**
   - Implemented dynamic compaction window calculation (`_plan_compaction_window`) with decaying `protect_first_n` (decays to 0 after first compaction to prevent early user turn fossilization, unless `pin_task_turn=True`), forward atomic tool boundary alignment (`align_boundary_forward`), and token-budget backward cut alignment (`align_boundary_backward`).
   - Implemented model-assisted summarizer and zero-dependency structured deterministic fallback summarizer adhering to strict markdown headings (`## CONTEXT COMPACTION SUMMARY`, `### Active Goals & User Intent`, `### Actions Taken & Files Modified`, `### Key Findings, Errors & Tool Results`, `### Current Working State`, and `--- END OF CONTEXT SUMMARY ---`).
   - Implemented in-flight task restatement (`_INFLIGHT_TASK_REPLAY_HEADER`): when the latest actionable user request is enclosed within the compacted region and the tail lacks a user prompt, the active goal is restated past the summary boundary to preserve agent intent.
   - Enforced anti-thrashing safety checks: reverts to original messages if compaction fails to reduce token footprint.

2. **Phase 4 Immutable Prompt-Cache Prefixing & Provider Breakpoints:**
   - Implemented `stabilize_prompt_cache_prefix` enforcing bit-for-bit canonical system instructions at index 0 and removing dynamic timestamps from static prefixes.
   - Implemented `compute_cache_signature` producing deterministic SHA-256 digests over tool schemas and the static prefix, verified to remain 100% constant across continuous turns.
   - Implemented `apply_cache_control_breakpoints` injecting Anthropic ephemeral cache breakpoints (`"cache_control": {"type": "ephemeral"}`) onto the last tool definition, system instructions, middle summary block, and conversation tail turn (respecting the 4-breakpoint limit).

3. **Runtime Integration & End-to-End Verification:**
   - Wired automatic compaction into `ReActLoop.run()` when estimated tokens exceed 80% of budget.
   - Exposed `get_context_compactor()` and `compact_context()` on `TwoTierModelRouter`.
   - Wired `ContextCompactor` into `HarnessOrchestrator.run_agent_loop()`.
   - Added `/compress` command and compaction status indicators (`[COMPACTED: -42% tokens]`) to `HexConsole` TUI.
   - Authored 6 end-to-end integration tests in `tests/test_context_compressor_integration.py`.

---

## 2. Tasks Completed

| Task | Description | Status | Commit |
|------|-------------|--------|--------|
| Task 1 | Middle-Turn Summarizer & ContextCompactor Core Engine | Complete | `c08d92c` |
| Task 2 | Immutable Prompt-Cache Prefixing & Provider Cache Breakpoints | Complete | `24ecbfa` |
| Task 3 | ReActLoop, Router & TUI Integration with End-to-End Test Suite | Complete | `27d1a47` |

---

## 3. Verification Proof

### Task 1: Middle-Turn Summarizer & ContextCompactor Core Engine
```bash
.venv/bin/python -c "from harness_core.context_compressor import ContextCompactor, CompactionConfig; compactor = ContextCompactor(CompactionConfig(max_context_tokens=1000, trigger_threshold=0.5)); msgs = [{'role': 'system', 'content': 'System prompt'}] + [{'role': 'user', 'content': f'Turn {i}: do something lengthy and detailed ' * 20} for i in range(15)]; res = compactor.compact_messages(msgs); assert res.compacted_tokens < res.original_tokens, 'Compaction did not reduce tokens'; print('Phase 3 Middle-Turn Summarizer verified. Ratio:', round(res.compression_ratio, 2))"
```
**Output:**
```
Phase 3 Middle-Turn Summarizer verified. Ratio: 0.49
```

### Task 2: Immutable Prompt-Cache Prefixing & Provider Cache Breakpoints
```bash
.venv/bin/python -c "from harness_core.context_compressor import stabilize_prompt_cache_prefix, apply_cache_control_breakpoints, compute_cache_signature; msgs = [{'role': 'user', 'content': 'q1'}, {'role': 'assistant', 'content': 'a1'}]; tools = [{'name': 'read_file', 'description': 'read'}]; p_msgs = stabilize_prompt_cache_prefix(msgs, 'Sys instructions', tools); sig1 = compute_cache_signature(p_msgs, tools); p_msgs2 = stabilize_prompt_cache_prefix(msgs + [{'role': 'user', 'content': 'q2'}], 'Sys instructions', tools); sig2 = compute_cache_signature(p_msgs2, tools); assert sig1 == sig2, 'Cache prefix signature drifted!'; tagged_msgs, tagged_tools = apply_cache_control_breakpoints(p_msgs, tools, provider='anthropic'); assert tagged_tools[-1].get('cache_control') == {'type': 'ephemeral'}; print('Phase 4 Prompt-Cache Prefixing verified')"
```
**Output:**
```
Phase 4 Prompt-Cache Prefixing verified
```

### Task 3: Integration Test Suite
```bash
.venv/bin/python -m unittest tests/test_context_compressor_integration.py
```
**Output:**
```
Ran 6 tests in 3.175s
OK
```

### Context Compactor Suite (All 14 Tests)
```bash
.venv/bin/python -m unittest tests/test_context_compressor_tail_pruning.py tests/test_context_compressor_integration.py
```
**Output:**
```
Ran 14 tests in 3.192s
OK
```

---

## 4. Deviations & Bug Fixes

- **Cross-provider model ID validation in `TwoTierModelRouter`:** When `~/.hex/config.yaml` contained a model from a different vendor (e.g. `z-ai/glm-5.3-flash`), selecting `anthropic` or `openai` or `gemini` could inherit the non-matching model ID if it did not match the exclusion prefixes. Hardened `TwoTierModelRouter.__init__` to verify that when `anthropic` is selected, model IDs must start with `claude` (or be cleared for default selection `claude-3-5-sonnet`), and similarly for `openai` (`gpt`, `o1`, `o3`) and `gemini` (`gemini`). Verified that `tests/test_ux_audit_repairs.py` (17 tests) passed cleanly.

---

## 5. Artifacts Produced / Modified

- `src/harness_core/context_compressor.py`
- `src/harness_core/react_loop.py`
- `src/harness_core/router.py`
- `src/harness_core/orchestrator.py`
- `src/harness_core/tui.py`
- `tests/test_context_compressor_integration.py`
- `.gsd/phases/26-context-compactor/26-02-SUMMARY.md`
