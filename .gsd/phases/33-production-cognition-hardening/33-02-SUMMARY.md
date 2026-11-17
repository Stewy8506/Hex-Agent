# Phase 33 Plan 33-02 Summary: AST Memory Precision & Observation Fencing (Fixes F-03, F-07)

**Execution Date:** 2026-11-15  
**Status:** Complete & Fully Verified  
**Commits:**
- `1770efe` — `feat(phase-33): ast symbol body fingerprinting and sqlite fts5 node memory storage (F-03)`
- `f240cb5` — `feat(phase-33): observation fencing, byte caps, and write-time injection gate for jit memories (F-07)`

---

## 1. Key Accomplishments

### Task 1: AST Symbol Body Fingerprinting & SQLite FTS5 Storage (F-03)
- **Symbol Body Fingerprinting (`compute_code_fingerprint` & `resolve_symbol_span`)**:
  - Implemented exact definition span resolution `(lineno, end_lineno)` for Python AST (`ast.parse`), ActiveGraphify index lookup (`symbols_by_name`), and polyglot definition regex matching.
  - Hashing now isolates the specific symbol definition lines (`hashlib.sha256(symbol_body.encode("utf-8")).hexdigest()`), completely decoupling symbol staleness from edits to unrelated functions, docstrings, or module comments.
- **Symbol Staleness Detection (`is_fingerprint_stale`)**:
  - Compares the current symbol definition body hash against `stored_fingerprint`.
  - Unrelated edits to other functions in the same module return `is_stale=False`.
  - Modifying the symbol body or deleting the symbol returns `is_stale=True`.
- **SQLite FTS5 Storage (`node_memories.db`)**:
  - Migrated `ASTNodeMemoryStore` from flat JSON to SQLite database at `.hex/memories/node_memories.db` with WAL mode and `_file_lock` concurrency protection.
  - Created relational `node_memories` table with indexes on `(rel_path, symbol_name)` and `(qualified_symbol)`.
  - Created SQLite virtual table `node_memories_fts USING fts5(id UNINDEXED, symbol_name, rel_path, note, category)` with BM25 relevance ranking.
  - Implemented `search_node_memories(query, limit=10)` with query sanitization and fallback to `LIKE` queries.
  - Built automatic, one-time legacy migration from `.hex/memories/ast_memories.json` when the SQLite database is empty.
- **Compatibility Adapter (`graphify_adapter.py`)**:
  - Added `src/harness_core/graphify_adapter.py` re-exporting `ActiveGraphifyAdapter` and `SymbolDef` for clean modular imports.

### Task 2: Untrusted Observation Fencing, Byte Caps & Write-Time Injection Scanning (F-07)
- **Write-Time Prompt Injection Scanning (`record_node_memory`)**:
  - Scans proposed candidate notes using `scan_for_threats()`.
  - Rejects adversarial prompts (e.g. `ignore previous instructions`, `disregard rules`, `system:`, `override tools`, `dump credentials`) by raising `ValueError("Prompt injection pattern detected in memory note: ...")`.
- **Rigid XML Untrusted Observation Fencing (`format_jit_memories_block`)**:
  - Wrapped JIT tool memory blocks in `<retrieved-memory trusted="false" target="{rel_path}">`.
  - Prepend non-executable security notice explicitly labeling memories as non-authoritative reference observations that cannot override security policy or tool permissions.
  - Escapes nested or closing XML fence tags (e.g. `</retrieved-memory>` converted to `&lt;/retrieved-memory&gt;`) to eliminate boundary escape attacks.
- **Byte Budget & Priority Ordering**:
  - Enforced `max_note_bytes: int = 300` with automatic `... [truncated]` suffix for oversized notes.
  - Enforced `max_total_bytes: int = 2000` ceiling; drops trailing lower-priority notes when budget is exhausted.
  - Prioritized items: 1) direct fresh symbol notes (`gotcha`, `invariant`, `post_mortem`), 2) 1-hop caller/callee warnings, 3) general guardrails, 4) stale notes.
- **Gateway Integration (`_tool_read_file`)**:
  - Appends fenced JIT observation block to `read_file` response when memories exist.
  - Bypasses appending when `jit_block` is empty to prevent trailing blank sections.

---

## 2. Empirical Verification

### Verification Suite Results
- `tests/cognition/test_ast_precision_and_fencing.py`: 12/12 passing in 0.93s
  - `TestASTSymbolPrecision`:
    - `test_fingerprint_unrelated_function_edit_does_not_stale` PASSED
    - `test_fingerprint_target_symbol_edit_causes_staleness` PASSED
    - `test_fingerprint_target_symbol_deletion_causes_staleness` PASSED
  - `TestSQLiteFTSNodeMemoryStore`:
    - `test_sqlite_db_and_fts_table_initialization` PASSED
    - `test_sqlite_persistence_and_fts_search` PASSED
    - `test_legacy_json_auto_migration` PASSED
  - `TestUntrustedObservationFencing`:
    - `test_injection_write_time_prompt_injection_rejection` PASSED
    - `test_fencing_wrapping_and_security_notice` PASSED
    - `test_fencing_tag_escaping` PASSED
    - `test_byte_caps_per_note_and_total` PASSED
    - `test_fencing_priority_ranking_direct_symbol_first` PASSED
    - `test_fencing_gateway_read_file_injection` PASSED
- Full Memory & Prompt Suite Regression: 42/42 passing in 3.62s
  - `tests/cognition/test_ast_scoped_memory.py`: 8/8 passed
  - `tests/cognition/test_curated_memory.py`: 19/19 passed
  - `tests/cognition/test_amem_and_lifecycle.py`: 10/10 passed
  - `tests/cognition/test_prompt_builder.py`: 5/5 passed

---

## 3. Preserved Architecture & Invariants

1. **PromptBuilder Slot Order**: Prefix-cache stability and slot order (#1 Soul, #2 Mode, #3 Env, #4 Curated Memory, #5 Project Rules) remain completely preserved.
2. **Curated Memory Integrity**: File locking (`_file_lock`) and atomic replacement with snapshotting remain intact.
3. **Guardrail Capture**: Guardrails capture for repeated failed tool calls and rolled-back diffs is untouched.
4. **Zero Backward Regressions**: Existing `format_jit_memories_block` callers continue to receive expected header markers within the XML fence.
