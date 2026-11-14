# Plan 31-01 Summary: Dual Markdown Stores, Negative Guardrails & Frozen Turn-0 Snapshotting

**Phase:** 31 (AST-Scoped Curated Memory, Negative Guardrails & Lifecycle Engine)  
**Plan:** 31-01  
**Status:** Completed & Empirically Verified  

---

## 1. Objectives Delivered

1. **Dual Human-Readable Markdown Stores (`USER.md` & `MEMORY.md`):**
   - Implemented `CuratedMemoryStore` in `src/harness_core/memory.py` managing Developer DNA (`USER.md` capped at ~1,375 chars) in `HEX_HOME` and Project Architecture (`MEMORY.md` capped at ~2,200 chars) in workspace `.hex/memories/`.
   - Fact entries delimited cleanly by `ENTRY_DELIMITER = "\n§\n"`.
   - Support for `add`, `replace`, `patch`, `remove`, `list_entries`, and batch `operations`.
   - Tolerant quote/dash/whitespace normalization (`_normalize_for_match`) matching Nous Research Hermes Agent.
   - Atomic file replace using temporary files and cross-platform file locking (`fcntl` / `msvcrt`).
   - External drift detection with `.bak.<timestamp>` snapshots when manual edits produce un-delimited oversized entries.

2. **Negative Constraint Memory (`GUARDRAILS.md` with `[AVOID]` Invariants):**
   - Implemented `GuardrailsManager` in `src/harness_core/memory.py` managing `.hex/memories/GUARDRAILS.md` (~2,000 char cap with FIFO eviction).
   - Invariant format: `- [AVOID] <Actionable negative instruction>`.
   - Wired automatic failure capture in `src/harness_core/react_loop.py`:
     - Tracks tool failure signatures (`name:signature`).
     - Ignores transient errors (429 rate limits, 503 service unavailable, connection timeouts).
     - Captures deterministic repeat failures ($\ge 2$ consecutive times) and auto-synthesizes concise `[AVOID]` invariants into `GUARDRAILS.md`.
     - Supports operator rejected diffs (`record_rejected_diff`) and rollbacks (`record_rollback`).

3. **Frozen Turn-0 KV-Cache Snapshotting & Streaming Scrubber:**
   - Implemented `MemorySnapshot` in `src/harness_core/memory.py` assembling Turn-0 snapshot of `USER.md`, `MEMORY.md`, and `GUARDRAILS.md` into Slot #4 fenced by `<memory-context>`.
   - Section-scoped bullet deduplication (`_drop_repeated_recall_lines`).
   - 100% byte-identical KV-cache prefix stability guarantee: mid-session writes to disk update files atomically but never mutate active session prompt prefix.
   - Implemented stateful `StreamingContextScrubber` holding partial tag tails and stripping `<memory-context>...</memory-context>` spans from streaming tokens to user display.
   - Updated `PromptBuilder` in `src/harness_core/prompt_builder.py` and `ToolGateway._tool_memory` in `src/harness_core/gateway.py`.

---

## 2. Tasks & Commits

| Task | Name | Commit | Summary |
|------|------|--------|---------|
| Task 1 | Core `CuratedMemoryStore` & `StreamingContextScrubber` | `dab1d96` | Implemented bounded dual markdown stores, locking, drift detection, and stateful streaming tag scrubber in `memory.py` |
| Task 2 | Negative Constraint Memory & Failure Capture (`GUARDRAILS.md`) | `3a3b951` | Implemented `GuardrailsManager` and automated $\ge 2$ repeat tool failure capture in `ReActLoop` |
| Task 3 | Integration with PromptBuilder, ToolGateway & Verification | `70737f2` | Wired Slot #4 frozen snapshot, updated `ToolGateway._tool_memory` CRUD, and verified prompt hash stability |

---

## 3. Verification Evidence

- `tests/test_curated_memory.py`: 19/19 passing tests covering CRUD, char limits, tolerant replacement, patch ambiguity, drift backup, streaming chunk straddling, bullet deduplication, repeat failure auto-capture, transient error filtering, rejected diffs/rollbacks, gateway execution, and byte-exact KV-cache prompt hash invariance.
- `tests/test_react_loop.py`: 33/33 passing tests with zero regressions.
- Combined test run (`.venv/bin/pytest tests/test_curated_memory.py tests/test_react_loop.py -v`): 52/52 passing tests in 2.82s.
