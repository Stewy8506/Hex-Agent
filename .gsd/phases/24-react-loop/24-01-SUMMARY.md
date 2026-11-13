# Plan 24-01: Continuous Multi-Turn ReAct Loop Engine Core Summary

**Phase:** Phase 24 — Continuous Multi-Turn ReAct Tool Loop (Hermes Parity Pillar 1)  
**Task:** 24-01  
**Wave:** 1  
**Status:** Complete  

---

## 1. Objectives Delivered

1. **Created `src/harness_core/react_loop.py`**:
   - `ReActConfig`: dataclass configuring `max_turns` (default 30), `temperature` (default 0.2), `system_prompt`, `budget_tokens` (default 100,000), and `stop_sequences`.
   - `ToolCallRequest`: dataclass capturing `call_id`, `name`, and `arguments`.
   - `ToolCallResult`: dataclass capturing `call_id`, `name`, `output`, `is_error`, and `duration_ms`.
   - `ReActTurn`: dataclass tracking `turn_index`, `thought`, `tool_calls`, `tool_results`, and `latency_ms`.
   - `ReActSessionResult`: dataclass capturing `success`, `final_answer`, `total_turns`, `turns`, and `termination_reason` (`finish`, `max_turns`, `error`, `no_tool_calls`).
   - `ReActLoop`:
     - Synchronous `run(prompt, ...)` and asynchronous `run_async(prompt, ...)`.
     - Multi-format tool call parser supporting:
       - JSON structured responses with `"tool_calls": [{"name": ..., "arguments": ...}]` or `"tool": ..., "args": ...`.
       - XML/tag blocks: `<tool_call>\n{"name": "...", "arguments": {...}}\n</tool_call>` and `<tool_call name="...">`.
       - Markdown code fences with JSON containing tool calls (` ```json ... ``` `).
       - Top-level raw JSON strings and balanced-brace embedded JSON search.
     - Multi-turn execution loop:
       - Manages structured chat message history across turns.
       - Dispatches tool invocations to `gateway.tools[name].handler(workspace_root, arguments)`.
       - Captures execution duration and isolates tool runtime errors.
       - Records and seals steps/tool calls in `LedgerStore` if provided.
       - Formats tool observations (`<observation tool="...">...</observation>`) back to message history for subsequent model turns.
       - Detects `<finish>` completion tags and natural final answers (no tool calls).
       - Enforces `max_turns` exhaustion and handles model caller exceptions gracefully.

2. **Created `tests/test_react_loop.py`**:
   - `TestReActDataStructures`: Validates default parameters and data structure initializations.
   - `TestToolCallParsing`: Comprehensive test suite testing parsing from dictionaries, single tool objects, XML tags, markdown code blocks, raw JSON strings, embedded JSON, and pure thoughts.
   - `TestMultiTurnReActLoop`:
     - Multi-turn cycle (read_file -> write_file -> finish).
     - Natural final answer termination (`no_tool_calls`).
     - Max turns limit reached (`max_turns`).
     - Tool execution error recovery (model receives error observation and adapts).
     - Unknown tool handling.
     - Model caller exception handling (`error`).
   - `TestLedgerIntegration`: Step/tool call recording with mock ledger and SQLite `LedgerStore` integration.
   - `TestAsyncAndRealGateway`: Async execution with `run_async` and end-to-end integration with real `ToolGateway` reading/listing workspace files.

---

## 2. Empirical Verification

- Target test execution:
  ```bash
  .venv/bin/python -m unittest tests/test_react_loop.py
  ```
  **Result:** 21/21 tests passed (0.80s, OK).

---

## 3. Commits

- `2bb6d6e`: feat(phase-24): create react loop engine core in react_loop.py
- `fa1cd46`: feat(phase-24): author react loop unit test suite and parsing robustness
